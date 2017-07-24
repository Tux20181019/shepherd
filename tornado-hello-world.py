# -*- coding: utf-8 -*-

import os
import shutil
import json
import logging
import logging.config
import subprocess
import MySQLdb
from MySQLdb.cursors import DictCursor

from StringIO import StringIO
import pycurl
from urllib import urlencode

from datetime import datetime
from datetime import timedelta

import tornado.web
from tornado.ioloop import IOLoop

from apscheduler.schedulers.tornado import TornadoScheduler
from threading import Lock

from crawl_job import CrawlJob


class SpiderConfigRequestHandler(tornado.web.RequestHandler):
    def __init__(self, *args, **kwargs):
        super(SpiderConfigRequestHandler, self).__init__(*args, **kwargs)

        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info("Instance created.")
        self.shepherd = shepherd

    def post(self, *args, **kwargs):
        user_id = self.get_argument('user_id')
        job_id = self.get_argument('job_id')

        self.logger.debug("Getting job configuration for user %s and job %s", user_id, job_id)

        conn = MySQLdb.connect(host=os.getenv('SHEPHERD_DB_HOST'), port=3306,
                               user=os.getenv('SHEPHERD_DB_USER'),
                               passwd=os.getenv('SHEPHERD_DB_PASS'),
                               cursorclass=DictCursor, charset='utf8')

        job = CrawlJob().load(int(user_id), int(job_id), conn)

        self.logger.debug(job.fields)

        json_data = json.dumps(job.fields, indent=4)
        self.write(json_data)

        conn.close()


class UpdateStatusRequestHandler(tornado.web.RequestHandler):
    def __init__(self, *args, **kwargs):
        super(UpdateStatusRequestHandler, self).__init__(*args, **kwargs)

        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info("Instance created.")

        self.shepherd = shepherd

    def post(self, *args, **kwargs):
        stats_info = {
            'user_id': self.get_argument('user_id'),
            'job_id': self.get_argument('job_id'),
            'start_time': self.get_argument('start_time'),
            'run_status': self.get_argument('run_status'),
            'download_num': self.get_argument('download_num'),
            'pending_num': self.get_argument('pending_num'),
            'error_num': self.get_argument('error_num'),
        }

        self.logger.debug(stats_info)

        self.save_stats_info(stats_info)

    def save_stats_info(self, stats_info):
        conn = None
        try:
            conn = MySQLdb.connect(host=os.getenv('SHEPHERD_DB_HOST'), port=3306,
                                   user=os.getenv('SHEPHERD_DB_USER'),
                                   passwd=os.getenv('SHEPHERD_DB_PASS'),
                                   cursorclass=DictCursor, charset='utf8')
            conn.autocommit(True)

            sql = '''insert into {db}.{table_name} (
                          user_id, job_id, start_time, run_status, download_page_num,
                          pending_page_num, error_page_num)
                      values (%s, %s, %s, %s, %s, %s, %s)
                      on duplicate key update
                        run_status = %s,
                        download_page_num = %s,
                        pending_page_num = %s,
                        error_page_num = %s
                      '''.format(db=os.getenv('SHEPHERD_DB_NAME'), table_name='crawl_status')

            cursor = conn.cursor()

            sql_param = (
                stats_info['user_id'],
                stats_info['job_id'],
                stats_info['start_time'],
                stats_info['run_status'],
                stats_info['download_num'],
                stats_info['pending_num'],
                stats_info['error_num'],
                stats_info['run_status'],
                stats_info['download_num'],
                stats_info['pending_num'],
                stats_info['error_num'],
            )

            cursor.execute(sql, sql_param)

            cursor.close()
        except Exception:
            self.logger.exception("Failed to save stats info.")
        finally:
            conn.close()


class JobControlRequestHandler(tornado.web.RequestHandler):
    def __init__(self, *args, **kwargs):

        super(JobControlRequestHandler, self).__init__(*args, **kwargs)

        self.logger = logging.getLogger(self.__class__.__name__)

        self.commands = {
            'start': self.start_job,
            'stop': self.stop_job,
            'schedule': self.schedule_job
        }

        self.shepherd = shepherd

    def post(self, *args, **kwargs):

        self.commands[self.get_argument('command')]()

    def schedule_job(self):
        pass

    def stop_job(self):
        user_id = self.get_argument('user_id')
        job_id = self.get_argument('job_id')

        try:
            self.shepherd.job_controller.stop_job(user_id, job_id)
        except Exception as e:
            self.logger.exception("Error:")
            self.write(e.message if len(e.message) > 0 else 'Exception')
        else:
            self.write("SUCCEED")

    def start_job(self):
        user_id = self.get_argument('user_id')
        job_id = self.get_argument('job_id')

        try:
            self.shepherd.job_controller.start_job(user_id, job_id)
        except Exception as e:
            self.logger.exception("Error:")
            self.write(e.message if len(e.message) > 0 else 'Exception')
        else:
            self.write("SUCCEED")


class JobController(object):
    def __init__(self):
        super(JobController, self).__init__()
        self.logger = logging.getLogger(self.__class__.__name__)

        self.conn = MySQLdb.connect(host=os.getenv('SHEPHERD_DB_HOST'), port=3306,
                                    user=os.getenv('SHEPHERD_DB_USER'),
                                    passwd=os.getenv('SHEPHERD_DB_PASS'),
                                    cursorclass=DictCursor, charset='utf8')
        self.conn.autocommit(True)

        self.lock = Lock()

    def call_scrapyd(self, host_ip, api, post_data=None, get_param=None):

        fields = None
        if post_data is not None:
            fields = urlencode(post_data)

        buf = StringIO()

        curl = pycurl.Curl()

        url = 'http://%s:6800/%s' % (host_ip, api)
        if get_param is not None and len(get_param) > 0:
            sep = '?'
            for param in get_param.keys():
                url = url + sep + param + '=' + get_param[param]
                sep = '&'

        curl.setopt(curl.URL, url)
        curl.setopt(curl.WRITEDATA, buf)
        if post_data is not None and len(post_data) > 0:
            curl.setopt(curl.POSTFIELDS, fields)

        curl.perform()

        result = json.loads(buf.getvalue())

        curl.close()

        if result['status'] != 'ok':
            raise Exception("Call scrapyd api %s failed." % api)

        return result

    def job_redploy(self, job_path, job_name, host_ip):

        result = self.call_scrapyd(host_ip, 'listprojects.json')
        self.logger.debug("Current deployed projects are: %s", result['projects'])

        if job_name in result['projects']:
            self.logger.debug("Removing the existing project %s on host %s", job_name, host_ip)
            self.call_scrapyd(host_ip, 'delproject.json', post_data={'project': job_name})

        os.chdir(job_path)
        self.logger.debug("Deploying project %s to %s", job_name, host_ip)
        ret = subprocess.call(['scrapyd-deploy', '-p', job_name])
        if ret != 0:
            raise Exception("Failed to deploy project %s to %s", job_name, host_ip)

    def job_create(self, user_id, job_id):

        job_template_path = os.getenv('SHEPHERD_SPIDER_TEMPLATE')
        if job_template_path is None or len(job_template_path) == 0:
            raise Exception("SHEPHERD_SPIDER_TEMPLATE is not defined.")

        if not os.path.isdir(job_template_path):
            raise Exception("Spider template must be a directory.")

        job_prepare_path = os.getenv('SHEPHERD_JOB_PREPARE_PATH')
        if job_prepare_path is None or len(job_prepare_path) == 0:
            raise Exception("SHEPHERD_JOB_PREPARE_PATH is not defined.")

        if not os.path.isdir(job_prepare_path):
            if not os.path.exists(job_prepare_path):
                os.mkdir(job_prepare_path)
            else:
                raise Exception("Spider template must be a directory.")

        job_name = 'job_%s_%s' % (user_id, job_id)
        job_path = job_prepare_path + os.path.sep + job_name
        if os.path.exists(job_path):
            self.logger.warning("%s is existing, delete it.", job_path)
            shutil.rmtree(job_path)

        os.mkdir(job_path)

        shutil.copytree(job_template_path, job_path + os.path.sep + 'clematis')

        os.chdir(job_path)

        host_info = self.get_host_info(user_id, job_id)

        self.create_scrapy_cfg(job_path, host_info['host_ip'], job_name)

        self.adjust_scrapy_settings(job_path, user_id, job_id)

        return job_path, job_name, host_info['host_ip']

    def adjust_scrapy_settings(self, job_path, user_id, job_id):
        settings = job_path + os.path.sep + 'clematis' + os.path.sep + 'settings.py'

        f = open(settings, "a")
        f.write('USER_ID = %s\n' % user_id)
        f.write('JOB_ID = %s\n' % job_id)
        f.close()

    def get_host_info(self, user_id, job_id):

        sql = '''select host.* from {db}.crawl_job job, {db}.crawl_host host 
                  where job.user_id=%s and job.job_id=%s
                  and job.host_id = host.host_id'''.format(db=os.getenv('SHEPHERD_DB_NAME'))

        cursor = self.conn.cursor()
        cursor.execute(sql, (user_id, job_id))

        host_info = cursor.fetchone()

        cursor.close()

        return host_info

    def create_scrapy_cfg(self, job_path, host_ip, job_name):

        cfg_file = job_path + os.path.sep + 'scrapy.cfg'
        if os.path.exists(cfg_file):
            os.remove(cfg_file)

        f = open(cfg_file, "w")

        f.write('[settings]\n')
        f.write('default = clematis.settings\n')
        f.write('[deploy]\n')
        f.write('url = http://%s:6800/\n' % host_ip)
        f.write('project = %s\n' % job_name)

        f.close()

    def start_job(self, user_id, job_id):

        with self.lock:
            working_dir = os.getcwd()

            try:
                self.logger.info("Refreshing job for user %s and job %s.", user_id, job_id)
                job_path, job_name, host_ip = self.job_create(user_id, job_id)

                self.job_redploy(job_path, job_name, host_ip)

                self.logger.info("Execute job %s on host %s immediately.", job_name, host_ip)
                results = self.call_scrapyd(host_ip, 'schedule.json',
                                            post_data={'project': job_name, 'spider': 'spider2'})
                if results['status'] != 'ok':
                    raise Exception('Failed to execute job %s on host %s', job_name, host_ip)

            except Exception:
                raise
            finally:
                os.chdir(working_dir)

    def stop_job(self, user_id, job_id):

        with self.lock:
            working_dir = os.getcwd()
            job_ids = []

            try:
                self.logger.info("Checking job status for user %s and job %s.", user_id, job_id)
                host_info = self.get_host_info(user_id, job_id)
                job_name = 'job_%s_%s' % (user_id, job_id)
                results = self.call_scrapyd(host_info['host_ip'], 'listjobs.json', get_param={'project': job_name})
                for job in results['running'] + results['pending']:
                    job_ids.append(job['id'])

                self.logger.info("The jobs to be terminated are %s", job_ids)
                for job_id in job_ids:
                    self.call_scrapyd(host_info['host_ip'], 'cancel.json',
                                      post_data={'project': job_name, 'job': job_id})

            except Exception:
                raise
            finally:
                os.chdir(working_dir)

    def schedule_job(self, trigger, trigger_args):
        with self.lock:
            pass


class Shepherd(object):
    def __init__(self):
        super(Shepherd, self).__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.scheduler = TornadoScheduler()

        self.application = tornado.web.Application([
            (r"/spider-config", SpiderConfigRequestHandler),
            (r"/update-status", UpdateStatusRequestHandler),
            (r"/job-control", JobControlRequestHandler),
        ])

        self.conn = MySQLdb.connect(host=os.getenv('SHEPHERD_DB_HOST'), port=3306,
                                    user=os.getenv('SHEPHERD_DB_USER'),
                                    passwd=os.getenv('SHEPHERD_DB_PASS'),
                                    cursorclass=DictCursor, charset='utf8')
        self.conn.autocommit(True)

        self.job_controller = JobController()

        self.job_schedules = {}

    def run(self):

        self.load_jobs()

        self.logger.debug(self.job_schedules)

        self.scheduler.start()

        self.scheduler.add_job(self.schedule_jobs, 'date', run_date=datetime.now() + timedelta(seconds=5))

        self.application.listen(8888)

        IOLoop.instance().start()

    def load_jobs(self):
        cursor = self.conn.cursor()

        sql = 'select j.user_id, j.job_id, j.job_schedule_id, s.job_schedule_type \
                  from {db}.crawl_job j, {db}.job_schedule s \
                  where j.job_schedule_id = s.job_schedule_id \
                  and j.is_valid = 1'.format(db=os.getenv('SHEPHERD_DB_NAME'))

        cursor.execute(sql)

        for row in cursor.fetchall():
            self.job_schedules[(row['user_id'], row['job_id'])] = {
                'job_schedule_id': row['job_schedule_id'], 'job_schedule_type': row['job_schedule_type']
            }

        cursor.close()

        sql = 'select * from {db}.job_schedule_param where job_schedule_id = %s'.format(
            db=os.getenv('SHEPHERD_DB_NAME'))

        for job in self.job_schedules.keys():
            cursor = self.conn.cursor()
            cursor.execute(sql, (self.job_schedules[job]['job_schedule_id']))
            for row in cursor.fetchall():
                self.job_schedules[job][row['param_name']] = row['param_value']
            cursor.close()

    def schedule_jobs(self):
        def schedule_by_datetime(job):
            if 'time' not in self.job_schedules[job] or self.job_schedules[job]['time'] == '':
                self.logger.debug('Starting job_%d_%d immediately.', job[0], job[1])
                self.job_controller.start_job(job[0], job[1])
            else:
                self.logger.debug("Job_%d_%d will be started at %s.",
                                  job[0], job[1], self.job_schedules[job]['time'])
                self.scheduler.add_job(self.job_controller.start_job,
                                       'date', run_date=self.job_schedules[job]['time'],
                                       args=[job[0], job[1]])

        def schedule_by_interval(job):
            self.logger.debug("Job_%d_%d will be scheduled at interval:", job[0], job[1])
            scheduler_args = {}
            for param in self.job_schedules[job].keys():
                if param != 'job_schedule_id' and param != 'job_schedule_type':
                    self.logger.debug('%s=%s', param, self.job_schedules[job][param])
                    if param != 'start_date' and param != 'end_date':
                        scheduler_args[param] = int(self.job_schedules[job][param])
                    else:
                        scheduler_args[param] = self.job_schedules[job][param]
            self.scheduler.add_job(self.job_controller.start_job, 'interval', args=[job[0], job[1]], **scheduler_args)

        def schedule_by_cron(job):
            pass

        schedulers = {
            0: schedule_by_datetime,
            1: schedule_by_interval,
            2: schedule_by_cron
        }

        for job in self.job_schedules.keys():
            schedulers[self.job_schedules[job]['job_schedule_type']](job)


class JobScheduler(object):
    def __init__(self):
        super(JobScheduler, self).__init__()


class MyJob(object):
    def __init__(self, scheduler):
        super(MyJob, self).__init__()
        self.scheduler = scheduler
        self.count = 0

    def run_job(self):
        print('\nTick! The time is %s' % datetime.now())
        self.count += 1
        # if self.count == 5:
        #     self.scheduler.add_job(self.run_another_job, trigger='interval', seconds=2, kwargs={'arg1': '12234'})

    def run_another_job(self, arg1):
        print ('\nAnother Tick with arg1=%s ! The time is %s' % (arg1, datetime.now()))


if __name__ == "__main__":
    logging.config.fileConfig(os.environ['SPIDER_LOGGING_CONF'], disable_existing_loggers=False)

    shepherd = Shepherd()

    shepherd.run()
