"""
TODO
    -Mongo wrapper to work w/ Controller._check_flag()
    -Use Mongo wrapper to set flags to start/stop threads
    -Use Mongo wrapper to work w/ Controller._collect()
"""

import sys, time, os, atexit, signal
import importlib
from bson.objectid import ObjectId

from db import DB

wd = os.path.join(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(wd)

class ProcessDaemon(object):

    def __init__(self, module, process, script, pidfile, stdin, stdout, stderr, home_dir='.', umask=022, verbose=1):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.pidfile = pidfile
        self.home_dir = home_dir
        self.verbose = verbose
        self.umask = umask
        self.daemon_alive = True

        self.module = module
        self.process = process
        self.script = script

        self.connection = DB()

        try:
            self.scriptd = importlib.import_module('%s.%s' % (self.module, self.script))
        except ImportError, error:
            print 'ImportError: %s' % error
            sys.exit(1)

    def daemonize(self):
        """
        Do the UNIX double-fork magic, see Stevens' "Advanced
        Programming in the UNIX Environment" for details (ISBN 0201563177)
        http://www.erlenstar.demon.co.uk/unix/faq_2.html#SEC16
        """
        try:
            pid = os.fork()
            if pid > 0:
                # Exit first parent
                sys.exit(0)
        except OSError, e:
            sys.stderr.write(
                "fork #1 failed: %d (%s)\n" % (e.errno, e.strerror))
            sys.exit(1)

        # Decouple from parent environment
        os.chdir(self.home_dir)
        os.setsid()
        os.umask(self.umask)

        # Do second fork
        try:
            pid = os.fork()
            if pid > 0:
                # Exit from second parent
                sys.exit(0)
        except OSError, e:
            sys.stderr.write(
                "fork #2 failed: %d (%s)\n" % (e.errno, e.strerror))
            sys.exit(1)

        sys.stdout.flush()
        sys.stderr.flush()
        si = file(self.stdin, 'r+')
        so = file(self.stdout, 'a+')
        if self.stderr:
            se = file(self.stderr, 'a+', 0)
        else:
            se = so
        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(so.fileno(), sys.stdout.fileno())
        os.dup2(se.fileno(), sys.stderr.fileno())

        def sigtermhandler(signum, frame):
            self.daemon_alive = False
            signal.signal(signal.SIGTERM, sigtermhandler)
            signal.signal(signal.SIGINT, sigtermhandler)

        if self.verbose >= 1:
            print "Started"

        # Write pidfile
        atexit.register(
            self.delpid)  # Make sure pid file is removed if we quit
        pid = str(os.getpid())
        file(self.pidfile, 'w+').write("%s\n" % pid)

    def delpid(self):
        os.remove(self.pidfile)

    def start(self, api=None, **kwargs):
        """
        Start the daemon
        """
        print 'Initializing...'
        if self.process in ['process', 'insert']:
            project_id = kwargs['project_id']
            run = kwargs['run']
            process = kwargs['process']
            insert = kwargs['insert']

            status = self.connection.set_network_status(project_id, self.module, run, process, insert)
        else:
            project_id = kwargs['project_id']
            collector_id = kwargs['collector_id']
            collector_status = kwargs['collector_status']

            status = self.connection.set_network_status(project_id, collector_id, collector_status)

        if status:
            print 'Flags set. Now starting daemon...'
        else:
            print 'Failed to successfully set flags, try again.'
            sys.exit()

        pid = self.get_pid()
        if pid:
            message = "pidfile %s already exists. Is it already running?\n"
            sys.stderr.write(message % self.pidfile)
            sys.exit(1)

        # Start the daemon
        self.daemonize()
        self.run(api)

    def stop(self, **kwargs):
        """
        Stop the daemon
        """
        # Finds project db w/ flags
        project_id = kwargs['project_id']
        project_info = self.get_project_detail(project_id)
        configdb = project_info['project_config_db']
        # Makes collection connection
        project_config_db = self.connection[configdb]
        coll = project_config_db.config

        print 'Setting flags to stop...'
        if self.process in ['process', 'insert']:
            run = kwargs['run']
            process = kwargs['process']
            insert = kwargs['insert']

            status = self.connection.set_network_status(project_id, self.module, run, process, insert)

            module_conf = coll.find_one({'module': self.module})
            if self.process == 'process':
                active = module_conf['processor_active']
            else:
                active = module_conf['inserter_active']
        else:
            collector_id = kwargs['collector_id']
            collector_status = kwargs['collector_status']

            status = self.connection.set_network_status(project_id, collector_id, collector_status)

            collector_conf = coll.find_one({'_id': ObjectId(project_id)})
            active = collector_conf['active']

        if status:
            print 'Flags set. Waiting for thread termination'
        else:
            print 'Failed to successfully set flags, try again.'
            sys.exit()

        # Loops thru checking active count 20 times to see if terminated
        # nicely.
        # Else goes to terminate immediately
        wait_count = 0
        while active == 1:
            wait_count += 1

            if self.process in ['process', 'insert']:
                module_conf = coll.find_one({'module': self.module})
                if self.process == 'process':
                    active = module_conf['processor_active']
                else:
                    active = module_conf['inserter_active']
            else:
                collector_conf = coll.find_one({'_id': ObjectId(project_id)})
                active = collector_conf['active']

            if wait_count > 20:
                break

            time.sleep(wait_count)

        # Get the pid from the pidfile
        pid = self.get_pid()
        if not pid:
            print "Daemon successfully stopped via thread termination."

            # Just to be sure. A ValueError might occur if the PID file is
            # empty but does actually exist
            if os.path.exists(self.pidfile):
                os.remove(self.pidfile)

            return  # Not an error in a restart

        # Try killing the daemon process
        try:
            i = 0
            while 1:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.1)
                i = i + 1
                if i % 10 == 0:
                    os.kill(pid, signal.SIGHUP)
        except OSError, err:
            err = str(err)
            if err.find("No such process") > 0:
                if os.path.exists(self.pidfile):
                    os.remove(self.pidfile)
            else:
                print str(err)
                sys.exit(1)

        print 'Daemon still running w/ loose thread. Stopping now...'
        print 'Stopped.'

    def restart(self, *args, **kwargs):
        """
        Restart the daemon
        """
        self.stop()
        self.start(*args, **kwargs)

    def get_pid(self):
        try:
            pf = file(self.pidfile, 'r')
            pid = int(pf.read().strip())
            pf.close()
        except IOError:
            pid = None
        except SystemExit:
            pid = None
        return pid

    def run(self, api):
        if self.process in ['process', 'insert']:
            self.scriptd.go()
        elif self.process == 'run':
            self.scriptd.go(api)
        else:
            print 'Unrecognized process type!'
            sys.exit(1)

class Controller():

    def __init__(self, project_id, collector_id, network):
        self.connection = DB()

        self.project_id = project_id
        self.collector_id = collector_id

        collector = self.connection.get_collector_detail(project_id, collector_id)
        if collector is None:
            print 'Collector (ID: %s) not found!' % collector_id
        else:
            self.module = collector['network']
            self.api = collector['api']

            network = self.connection.get_network_detail(project_id, self.module)
            if network is None:
                print 'Network %s not found!' % self.module
            else:
                self.collector = network['collection_script']
                self.processor = network['processor_script']
                self.inserter = network['insertion_script']

        self.usage_message = '[network-module] run|collect|process|insert start|stop|restart'

    def run(self, process, command):
        if process == 'run'     : self.initiate(command)
        if process == 'process' : self.process(command)
        if process == 'insert'  : self.insert(command)

    """
    def check_flag(self, module):
        exception = None
        try:
            mongoConfigs = mongo_config.find_one({"module" : module})
            run_flag = mongoConfigs['run']

            if module in ['collector-follow', 'collector-track']:
                collect_flag = mongoConfigs['collect']
                update_flag = mongoConfigs['update']
            else:
                collect_flag = 0
                update_flag = 0
        except Exception, exception:
            logger.info('Mongo connection refused with exception: %s' % exception)

        return run_flag, collect_flag, update_flag
    """

    # Initiates the collector script for the given network API
    def initiate(self, command):
        pidfile = '/tmp/' + self.collector_id + '-' + self.module + '-' + self.api + '-collector-daemon.pid'
        stdout = wd + '/out/' + self.collector_id + '-' + self.module + '-' + self.api + '-collector-out.txt'
        stdin = wd + '/out/' + self.collector_id + '-' + self.module + '-' + self.api + '-collector-in.txt'
        stderr = wd + '/out/' + self.collector_id + '-' + self.module + '-' + self.api + '-collector-err.txt'

        rund = ProcessDaemon(module=self.module,
            process='run',
            script=self.collector,
            pidfile=pidfile,
            stdout=stdout,
            stdin=stdin,
            stderr=stderr
        )

        if not os.path.isfile(stdout):
            create_file = open(stdout, 'w')
            create_file.close()
        if not os.path.isfile(stdin):
            create_file = open(stdin, 'w')
            create_file.close()
        if not os.path.isfile(stderr):
            create_file = open(stderr, 'w')
            create_file.close()

        if command not in ['start', 'stop', 'restart']:
            print 'Invalid command: %s' % command
            print 'USAGE: python %s %s' % (sys.argv[0], self.usage_message)
        elif command == 'start':
            rund.start(self.api, project_id=self.project_id, collector_id=self.collector_id, collector_status=1)
        elif command == 'stop':
            rund.stop()
        elif command == 'restart':
            rund.restart(self.api, project_id=self.project_id, collector_id=self.collector_id, collector_status=1)
        else:
            print 'USAGE: %s %s' % (sys.argv[0], self.usage_message)

    def process(self, command):
        pidfile = '/tmp/' + self.collector_id + '-' + self.module + '-' + self.api + '-processor-daemon.pid'
        stdout = wd + '/out/' + self.collector_id + '-' + self.module + '-' + self.api + '-processor-out.txt'
        stdin = wd + '/out/' + self.collector_id + '-' + self.module + '-' + self.api + '-processor-in.txt'
        stderr = wd + '/out/' + self.collector_id + '-' + self.module + '-' + self.api + '-processor-err.txt'

        processd = ProcessDaemon(module=self.module,
            process='process',
            script=self.processor,
            pidfile=pidfile,
            stdout=stdout,
            stdin=stdin,
            stderr=stderr
        )

        if not os.path.isfile(stdout):
            create_file = open(stdout, 'w')
            create_file.close()
        if not os.path.isfile(stdin):
            create_file = open(stdin, 'w')
            create_file.close()
        if not os.path.isfile(stderr):
            create_file = open(stderr, 'w')
            create_file.close()

        if command not in ['start', 'stop', 'restart']:
            print 'Invalid command: %s' % command
            print 'USAGE: %s %s' % (sys.argv[0], self.usage_message)
        elif command == 'start':
            processd.start(self.project_id, run=1, process=True, insert=False)
        elif command == 'stop':
            processd.stop()
        elif command == 'restart':
            processd.restart(self.project_id, run=1, process=True, insert=False)
        else:
            print 'USAGE: %s %s' % (sys.argv[0], self.usage_message)

    def insert(self, command):
        pidfile = '/tmp/' + self.collector_id + '-' + self.module + '-' + self.api + '-inserter-daemon.pid'
        stdout = wd + '/out/' + self.collector_id + '-' + self.module + '-' + self.api + '-inserter-out.txt'
        stdin = wd + '/out/' + self.collector_id + '-' + self.module + '-' + self.api + '-inserter-in.txt'
        stderr = wd + '/out/' + self.collector_id + '-' + self.module + '-' + self.api + '-inserter-err.txt'

        insertd = ProcessDaemon(module=self.module,
            process='process',
            script=self.inserter,
            pidfile=pidfile,
            stdout=stdout,
            stdin=stdin,
            stderr=stderr
        )

        if not os.path.isfile(stdout):
            create_file = open(stdout, 'w')
            create_file.close()
        if not os.path.isfile(stdin):
            create_file = open(stdin, 'w')
            create_file.close()
        if not os.path.isfile(stderr):
            create_file = open(stderr, 'w')
            create_file.close()

        if command not in ['start', 'stop', 'restart']:
            print 'Invalid command: %s' % command
            print 'USAGE: %s %s' % (sys.argv[0], self.usage_message)
        elif command == 'start':
            insertd.start(self.project_id, run=1, process=False, insert=True)
        elif command == 'stop':
            insertd.stop()
        elif command == 'restart':
            insertd.restart(self.project_id, run=1, process=False, insert=True)
        else:
            print 'USAGE: %s %s' % (sys.argv[0], self.usage_message)
