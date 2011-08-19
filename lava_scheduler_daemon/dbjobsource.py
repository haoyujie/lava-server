import datetime
import json
import logging

from django.core.files.base import ContentFile
from django.db import connection
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.utils import DatabaseError

from twisted.internet.threads import deferToThread

from zope.interface import implements

from lava_scheduler_app.models import Device, TestJob
from lava_scheduler_daemon.jobsource import IJobSource

try:
    from psycopg2 import InterfaceError, OperationalError
except ImportError:
    class InterfaceError(Exception):
        pass
    class OperationalError(Exception):
        pass


class DatabaseJobSource(object):

    implements(IJobSource)

    logger = logging.getLogger(__name__ + '.DatabaseJobSource')

    def getBoardList_impl(self):
        return [d.hostname for d in Device.objects.all()]

    def deferForDB(self, func, *args, **kw):
        def wrapper(*args, **kw):
            try:
                return func(*args, **kw)
            except (DatabaseError, OperationalError, InterfaceError), error:
                message = str(error)
                if message == 'connection already closed' or \
                   message.startswith(
                    'terminating connection due to administrator command') or \
                   message.startswith(
                    'could not connect to server: Connection refused'):
                    self.logger.warning(
                        'Forcing reconnection on next db access attempt')
                    if connection.connection:
                        if not connection.connection.closed:
                            connection.connection.close()
                        connection.connection = None
                raise
        return deferToThread(wrapper, *args, **kw)

    def getBoardList(self):
        return self.deferForDB(self.getBoardList_impl)

    @transaction.commit_manually()
    def getJobForBoard_impl(self, board_name):
        while True:
            device = Device.objects.get(hostname=board_name)
            if device.status != Device.IDLE:
                return None
            jobs_for_device = TestJob.objects.all().filter(
                Q(requested_device=device)
                | Q(requested_device_type=device.device_type),
                status=TestJob.SUBMITTED)
            jobs_for_device = jobs_for_device.extra(
                select={'is_targeted': 'requested_device_id is not NULL'},
                order_by=['-is_targeted', 'submit_time'])
            jobs = jobs_for_device[:1]
            if jobs:
                job = jobs[0]
                job.status = TestJob.RUNNING
                job.start_time = datetime.datetime.utcnow()
                job.actual_device = device
                device.status = Device.RUNNING
                device.current_job = job
                try:
                    # The unique constraint on current_job may cause this to
                    # fail in the case of concurrent requests for different
                    # boards grabbing the same job.  If there are concurrent
                    # requests for the *same* board they may both return the
                    # same job -- this is an application level bug though.
                    device.save()
                except IntegrityError:
                    transaction.rollback()
                    continue
                else:
                    job.log_file.save(
                        'job-%s.log' % job.id, ContentFile(''), save=False)
                    job.save()
                    json_data = json.loads(job.definition)
                    json_data['target'] = device.hostname
                    transaction.commit()
                    return json_data
            else:
                # We don't really need to rollback here, as no modifying
                # operations have been made to the database.  But Django is
                # stupi^Wconservative and assumes the queries that have been
                # issued might have been modifications.
                # See https://code.djangoproject.com/ticket/16491.
                transaction.rollback()
                return None

    def getJobForBoard(self, board_name):
        return self.deferForDB(self.getJobForBoard_impl, board_name)

    @transaction.commit_on_success()
    def getLogFileForJobOnBoard_impl(self, board_name):
        device = Device.objects.get(hostname=board_name)
        device.status = Device.IDLE
        job = device.current_job
        log_file = job.log_file
        log_file.file.close()
        log_file.open('wb')
        return log_file

    def getLogFileForJobOnBoard(self, board_name):
        return self.deferForDB(self.getLogFileForJobOnBoard_impl, board_name)

    @transaction.commit_on_success()
    def jobCompleted_impl(self, board_name):
        self.logger.debug('marking job as complete on %s', board_name)
        device = Device.objects.get(hostname=board_name)
        device.status = Device.IDLE
        job = device.current_job
        device.current_job = None
        if job.status == TestJob.RUNNING:
            job.status = TestJob.COMPLETE
        elif job.status == TestJob.CANCELING:
            job.status = TestJob.CANCELED
        else:
            self.logger.error(
                "Unexpected job state in jobCompleted: %s" % job.status)
            job.status = TestJob.COMPLETE
        job.end_time = datetime.datetime.utcnow()
        device.save()
        job.save()

    def jobCompleted(self, board_name):
        return self.deferForDB(self.jobCompleted_impl, board_name)

    @transaction.commit_on_success()
    def jobOobData_impl(self, board_name, key, value):
        self.logger.info(
            "oob data received for %s: %s: %s", board_name, key, value)
        if key == 'dashboard-put-result':
            device = Device.objects.get(hostname=board_name)
            device.current_job.results_link = value
            device.current_job.save()

    def jobOobData(self, board_name, key, value):
        return self.deferForDB(self.jobOobData_impl, board_name, key, value)

    def jobCheckForCancellation_impl(self, board_name):
        device = Device.objects.get(hostname=board_name)
        device.status = Device.IDLE
        job = device.current_job
        return job.status != TestJob.RUNNING

    def jobCheckForCancellation(self, board_name):
        return self.deferForDB(self.jobCheckForCancellation_impl, board_name)
