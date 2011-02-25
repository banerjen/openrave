"""
Overview
========

The multiprocess plugin enables you to distribute your test run among a set of
worker processes that run tests in parallel. This can speed up CPU-bound test
runs (as long as the number of work processeses is around the number of
processors or cores available), but is mainly useful for IO-bound tests that
spend most of their time waiting for data to arrive from someplace else.

.. note ::

   See :doc:`../doc_tests/test_multiprocess/multiprocess` for
   additional documentation and examples. Use of this plugin on python
   2.5 or earlier requires the multiprocessing_ module, also available
   from PyPI.

.. _multiprocessing : http://code.google.com/p/python-multiprocessing/

How tests are distributed
=========================

The ideal case would be to dispatch each test to a worker process
separately. This ideal is not attainable in all cases, however, because many
test suites depend on context (class, module or package) fixtures.

The plugin can't know (unless you tell it -- see below!) if a context fixture
can be called many times concurrently (is re-entrant), or if it can be shared
among tests running in different processes. Therefore, if a context has
fixtures, the default behavior is to dispatch the entire suite to a worker as
a unit.

Controlling distribution
^^^^^^^^^^^^^^^^^^^^^^^^

There are two context-level variables that you can use to control this default
behavior.

If a context's fixtures are re-entrant, set ``_multiprocess_can_split_ = True``
in the context, and the plugin will dispatch tests in suites bound to that
context as if the context had no fixtures. This means that the fixtures will
execute concurrently and multiple times, typically once per test.

If a context's fixtures can be shared by tests running in different processes
-- such as a package-level fixture that starts an external http server or
initializes a shared database -- then set ``_multiprocess_shared_ = True`` in
the context. These fixtures will then execute in the primary nose process, and
tests in those contexts will be individually dispatched to run in parallel.

How results are collected and reported
======================================

As each test or suite executes in a worker process, results (failures, errors,
and specially handled exceptions like SkipTest) are collected in that
process. When the worker process finishes, it returns results to the main
nose process. There, any progress output is printed (dots!), and the
results from the test run are combined into a consolidated result
set. When results have been received for all dispatched tests, or all
workers have died, the result summary is output as normal.

Beware!
=======

Not all test suites will benefit from, or even operate correctly using, this
plugin. For example, CPU-bound tests will run more slowly if you don't have
multiple processors. There are also some differences in plugin
interactions and behaviors due to the way in which tests are dispatched and
loaded. In general, test loading under this plugin operates as if it were
always in directed mode instead of discovered mode. For instance, doctests
in test modules will always be found when using this plugin with the doctest
plugin.

But the biggest issue you will face is probably concurrency. Unless you
have kept your tests as religiously pure unit tests, with no side-effects, no
ordering issues, and no external dependencies, chances are you will experience
odd, intermittent and unexplainable failures and errors when using this
plugin. This doesn't necessarily mean the plugin is broken; it may mean that
your test suite is not safe for concurrency.

"""
import logging
import os
import sys
import time
import traceback
import unittest
import pickle
import signal
import nose.case
from nose.core import TextTestRunner
from nose import failure
from nose import loader
from nose.plugins.base import Plugin
from nose.result import TextTestResult
from nose.suite import ContextSuite
from nose.util import test_address
try:
    # 2.7+
    from unittest.runner import _WritelnDecorator
except ImportError:
    from unittest import _WritelnDecorator
from Queue import Empty
from warnings import warn
try:
    from cStringIO import StringIO
except ImportError:
    import StringIO

import xunitmultiprocess, capture, callableclass

log = logging.getLogger(__name__)

Process = Queue = Pool = Event = Value = Array = None

class TimedOutException(Exception):
    def __init__(self, value = "Timed Out"):
        self.value = value
    def __str__(self):
        return repr(self.value)

def _import_mp():
    global Process, Queue, Pool, Event, Value, Array
    try:
        from multiprocessing import Process as Process_, \
            Queue as Queue_, Pool as Pool_, Event as Event_, Value as Value_, Array as Array_
        Process, Queue, Pool, Event, Value, Array = Process_, Queue_, Pool_, Event_, Value_, Array_
    except ImportError:
        warn("multiprocessing module is not available, multiprocess plugin "
             "cannot be used", RuntimeWarning)


class TestLet:
    def __init__(self, case):
        try:
            self._id = case.id()
        except AttributeError:
            pass
        self._short_description = case.shortDescription()
        self._str = str(case)

    def id(self):
        return self._id

    def shortDescription(self):
        return self._short_description

    def __str__(self):
        return self._str

class MultiProcess(Plugin):
    """
    Run tests in multiple processes. Requires processing module.
    """
    score = 1000
    status = {}

    def options(self, parser, env):
        """
        Register command-line options.
        """
        parser.add_option("--processes", action="store",
                          default=env.get('NOSE_PROCESSES', 0),
                          dest="multiprocess_workers",
                          metavar="NUM",
                          help="Spread test run among this many processes. "
                          "Set a number equal to the number of processors "
                          "or cores in your machine for best results. "
                          "[NOSE_PROCESSES]")
        parser.add_option("--process-timeout", action="store",
                          default=env.get('NOSE_PROCESS_TIMEOUT', 10),
                          dest="multiprocess_timeout",
                          metavar="SECONDS",
                          help="Set timeout for return of results from each "
                          "test runner process. [NOSE_PROCESS_TIMEOUT]")
        parser.add_option("--process-restartworker", action="store_true",
                          default=env.get('NOSE_PROCESS_RESTARTWORKER', False),
                          dest="multiprocess_restartworker",
                          help="If set, will restart each worker process once their tests are done, this helps control memory leaks from killing the system. [NOSE_PROCESS_RESTARTWORKER]")

    def configure(self, options, config):
        """
        Configure plugin.
        """
        try:
            self.status.pop('active')
        except KeyError:
            pass
        if not hasattr(options, 'multiprocess_workers'):
            self.enabled = False
            return
        # don't start inside of a worker process
        if config.worker:
            return
        self.config = config
        try:
            workers = int(options.multiprocess_workers)
        except (TypeError, ValueError):
            workers = 0
        if workers:
            _import_mp()
            if Process is None:
                self.enabled = False
                return
            self.enabled = True
            self.config.multiprocess_workers = workers
            self.config.multiprocess_timeout = int(options.multiprocess_timeout)
            self.config.multiprocess_restartworker = int(options.multiprocess_restartworker)
            self.status['active'] = True

    def prepareTestLoader(self, loader):
        """Remember loader class so MultiProcessTestRunner can instantiate
        the right loader.
        """
        self.loaderClass = loader.__class__

    def prepareTestRunner(self, runner):
        """Replace test runner with MultiProcessTestRunner.
        """
        # replace with our runner class
        return MultiProcessTestRunner(stream=runner.stream,
                                      verbosity=self.config.verbosity,
                                      config=self.config,
                                      loaderClass=self.loaderClass)

class MultiProcessTestRunner(TextTestRunner):
    def __init__(self, **kw):
        self.loaderClass = kw.pop('loaderClass', loader.defaultTestLoader)
        super(MultiProcessTestRunner, self).__init__(**kw)

    def run(self, test):
        """
        Execute the test (which may be a test suite). If the test is a suite,
        distribute it out among as many processes as have been configured, at
        as fine a level as is possible given the context fixtures defined in the
        suite or any sub-suites.

        """
        log.debug("%s.run(%s) (%s)", self, test, os.getpid())
        wrapper = self.config.plugins.prepareTest(test)
        if wrapper is not None:
            test = wrapper

        # plugins can decorate or capture the output stream
        wrapped = self.config.plugins.setOutputStream(self.stream)
        if wrapped is not None:
            self.stream = wrapped

        testQueue = Queue()
        resultQueue = Queue()
        tasks = []
        completed = []
        workers = []
        to_teardown = []
        shouldStop = Event()

        result = self._makeResult()
        start = time.time()

        # dispatch and collect results
        # put indexes only on queue because tests aren't picklable
        for case in self.nextBatch(test):
            log.debug("Next batch %s (%s)", case, type(case))
            if (isinstance(case, nose.case.Test) and
                isinstance(case.test, failure.Failure)):
                log.debug("Case is a Failure")
                case(result) # run here to capture the failure
                continue
            # handle shared fixtures
            if isinstance(case, ContextSuite) and self.sharedFixtures(case):
                log.debug("%s has shared fixtures", case)
                try:
                    case.setUp()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except:
                    log.debug("%s setup failed", sys.exc_info())
                    result.addError(case, sys.exc_info())
                else:
                    to_teardown.append(case)
                    for _t in case:
                        test_addr = self.addtask(testQueue,tasks,_t)
                        log.debug("Queued shared-fixture test %s (%s) to %s",
                                  len(tasks), test_addr, testQueue)

            else:
                test_addr = self.addtask(testQueue,tasks,case)
                log.debug("Queued test %s (%s) to %s",
                          len(tasks), test_addr, testQueue)

        log.debug("Starting %s workers", self.config.multiprocess_workers)
        for i in range(self.config.multiprocess_workers):
            currentaddr = Array('c',' '*1000)
            currentaddr.value = ''
            currentstart = Value('d')
            p = Process(target=runner, args=(i,
                                             testQueue,
                                             resultQueue,
                                             currentaddr,
                                             currentstart,
                                             shouldStop,
                                             self.loaderClass,
                                             result.__class__,
                                             pickle.dumps(self.config)))
            p.currentaddr = currentaddr
            p.currentstart = currentstart
            # p.setDaemon(True)
            p.start()
            workers.append(p)
            log.debug("Started worker process %s", i+1)

        total_tasks = len(tasks)
        # need to keep track of the next time to check for timeouts in case more than one process times out at the same time.
        nexttimeout=self.config.multiprocess_timeout
        while tasks:
            log.debug("Waiting for results (%s/%s tasks), timeout=%ds",
                      len(completed), total_tasks,self.config.multiprocess_timeout)
            try:
                iworker, addr, newtask_addrs, batch_result = resultQueue.get(timeout=nexttimeout)
                log.debug('Results received for %s, new tasks: %d', addr,len(newtask_addrs))
                try:
                    print addr
                    tasks.remove(addr)
                    total_tasks += len(newtask_addrs)
                    for newaddr in newtask_addrs:
                        tasks.append(newaddr)
                except KeyError:
                    log.debug("Got result for unknown task? %s", addr)
                    log.debug("current: %s",str(list(tasks)[0]))
                else:
                    completed.append([addr,batch_result])
                self.consolidate(result, batch_result)
                if (self.config.stopOnError
                    and not result.wasSuccessful()):
                    # set the stop condition
                    shouldStop.set()
                    break
                if self.config.multiprocess_restartworker:
                    success = workers[iworker].join()
            except Empty:
                log.debug("Timed out with %s tasks pending", len(tasks))
                any_alive = False
                for w in workers:
                    if w.is_alive():
                        worker_addr = w.currentaddr.value
                        timeprocessing = time.time()-w.currentstart.value
                        if len(worker_addr) > 0 and timeprocessing > self.config.multiprocess_timeout:
                            log.debug('timed out: %s',worker_addr)
                            w.currentaddr.value = ''
                            os.kill(w.pid, signal.SIGINT)
                        any_alive = True
                        break
                if not any_alive:
                    log.debug("All workers dead")
                    break
            # compute next timeout
            nexttimeout=self.config.multiprocess_timeout
            for w in workers:
                if w.is_alive() and len(w.currentaddr.value) > 0:
                    timeprocessing = time.time()-w.currentstart.value
                    if timeprocessing <= self.config.multiprocess_timeout:
                        nexttimeout = min(nexttimeout,self.config.multiprocess_timeout-timeprocessing)
            if self.config.multiprocess_restartworker and not shouldStop.is_set() and not testQueue.empty():
                # restart worker threads
                for i,w in enumerate(workers):
                    if w is None or not w.is_alive():
                        currentaddr = Array('c',' '*1000)
                        currentaddr.value = ''
                        currentstart = Value('d')
                        workers[i] = Process(target=runner, args=(i,
                                             testQueue,
                                             resultQueue,
                                             currentaddr,
                                             currentstart,
                                             shouldStop,
                                             self.loaderClass,
                                             result.__class__,
                                             pickle.dumps(self.config)))
                        workers[i].currentaddr = currentaddr
                        workers[i].currentstart = currentstart
                        workers[i].start()

        log.debug("Completed %s tasks (%s remain)", len(completed), len(tasks))

        for case in to_teardown:
            log.debug("Tearing down shared fixtures for %s", case)
            try:
                case.tearDown()
            except (KeyboardInterrupt, SystemExit):
                raise
            except:
                result.addError(case, sys.exc_info())

        stop = time.time()

        # first write since can freeze on shutting down processes
        result.printErrors()
        result.printSummary(start, stop)
        self.config.plugins.finalize(result)

        log.debug("Tell all workers to stop")
        for w in workers:
            if w.is_alive():
                testQueue.put('STOP', block=False)

        # wait for the workers to end
        try:
            for worker in workers:
                worker.join()
        except KeyboardInterrupt:
            print 'parent received ctrl-c'
            for worker in workers:
                worker.terminate()
                worker.join()

        return result

    @staticmethod
    def addtask(testQueue,tasks,case):
        arg = None
        if isinstance(case,nose.case.Test) and hasattr(case.test,'arg'):
            # this removes the top level descriptor and allows real function name to be returned
            #case.descriptor = None
            case.test.descriptor = None
            arg = case.test.arg
        #log.debug('adding (%s): %s '%(type(case),str(case)))
        test_addr = MultiProcessTestRunner.address(case)
        testQueue.put((test_addr,arg), block=False)
        if arg is not None:
            test_addr += str(arg)
        if tasks is not None:
            tasks.append(test_addr)
        return test_addr

    @staticmethod
    def address(case):
        if hasattr(case, 'address'):
            file, mod, call = case.address()
        elif hasattr(case, 'context'):
            file, mod, call = test_address(case.context)
        else:
            raise Exception("Unable to convert %s to address" % case)
        parts = []
        if file is None:
            if mod is None:
                raise Exception("Unaddressable case %s" % case)
            else:
                parts.append(mod)
        else:
            # strip __init__.py(c) from end of file part
            # if present, having it there confuses loader
            dirname, basename = os.path.split(file)
            if basename.startswith('__init__'):
                file = dirname
            parts.append(file)
        if call is not None:
            parts.append(call)
        return ':'.join(map(str, parts))

    def nextBatch(self, test):
        # allows tests or suites to mark themselves as not safe
        # for multiprocess execution
        if hasattr(test, 'context'):
            if not getattr(test.context, '_multiprocess_', True):
                return

        if ((isinstance(test, ContextSuite)
             and test.hasFixtures(self.checkCanSplit))
            or not getattr(test, 'can_split', True)
            or not isinstance(test, unittest.TestSuite)):
            # regular test case, or a suite with context fixtures

            # special case: when run like nosetests path/to/module.py
            # the top-level suite has only one item, and it shares
            # the same context as that item. In that case, we want the
            # item, not the top-level suite
            if isinstance(test, ContextSuite):
                contained = list(test)
                if (len(contained) == 1
                    and getattr(contained[0], 'context', None) == test.context):
                    test = contained[0]
            yield test
        else:
            # Suite is without fixtures at this level; but it may have
            # fixtures at any deeper level, so we need to examine it all
            # the way down to the case level
            for case in test:
                for batch in self.nextBatch(case):
                    yield batch

    def checkCanSplit(self, context, fixt):
        """
        Callback that we use to check whether the fixtures found in a
        context or ancestor are ones we care about.

        Contexts can tell us that their fixtures are reentrant by setting
        _multiprocess_can_split_. So if we see that, we return False to
        disregard those fixtures.
        """
        if not fixt:
            return False
        if getattr(context, '_multiprocess_can_split_', False):
            return False
        return True

    def sharedFixtures(self, case):
        context = getattr(case, 'context', None)
        if not context:
            return False
        return getattr(context, '_multiprocess_shared_', False)

    def consolidate(self, result, batch_result):
        log.debug("batch result is %s" , batch_result)
        try:
            output, testsRun, failures, errors, errorClasses = batch_result
        except ValueError:
            log.debug("result in unexpected format %s", batch_result)
            failure.Failure(*sys.exc_info())(result)
            return
        self.stream.write(output)
        result.testsRun += testsRun
        result.failures.extend(failures)
        result.errors.extend(errors)
        for key, (storage, label, isfail) in errorClasses.items():
            if key not in result.errorClasses:
                # Ordinarily storage is result attribute
                # but it's only processed through the errorClasses
                # dict, so it's ok to fake it here
                result.errorClasses[key] = ([], label, isfail)
            mystorage, _junk, _junk = result.errorClasses[key]
            mystorage.extend(storage)
        log.debug("Ran %s tests (total: %s)", testsRun, result.testsRun)


def runner(ix, testQueue, resultQueue, currentaddr, currentstart, shouldStop,
           loaderClass, resultClass, config):
    config = pickle.loads(config)
    dummy_parser = config.parserClass()

    # manually add xunitmultiprocess plugin
    for plugin in [capture.Capture(), xunitmultiprocess.Xunitmp(), callableclass.CallableClass()]:
        plugin.addOptions(dummy_parser,{})
        config.plugins.addPlugin(plugin)
    config.plugins.addPlugin(plugin)
    config.plugins.configure(config.options,config)
    config.plugins.begin()
    log.debug("Worker %s executing", ix)
    loader = loaderClass(config=config)
    loader.suiteClass.suiteClass = NoSharedFixtureContextSuite

    def get():
        return testQueue.get(timeout=config.multiprocess_timeout)

    def makeResult():
        stream = _WritelnDecorator(StringIO())
        result = resultClass(stream, descriptions=1,
                             verbosity=config.verbosity,
                             config=config)
        plug_result = config.plugins.prepareTestResult(result)
        if plug_result:
            return plug_result
        return result

    def batch(result):
        failures = [(TestLet(c), err) for c, err in result.failures]
        errors = [(TestLet(c), err) for c, err in result.errors]
        errorClasses = {}
        for key, (storage, label, isfail) in result.errorClasses.items():
            errorClasses[key] = ([(TestLet(c), err) for c, err in storage],
                                 label, isfail)
        return (
            result.stream.getvalue(),
            result.testsRun,
            failures,
            errors,
            errorClasses)
    try:
        try:
            for test_addr, arg in iter(get, 'STOP'):
                if shouldStop.is_set():
                    break
                result = makeResult()
                test = loader.loadTestsFromNames([test_addr])
                test.testQueue = testQueue
                test.tasks = []
                test.arg = arg
                log.debug("Worker %s Test is %s (%s)", ix, test_addr, test)
                try:
                    if arg is not None:
                        test_addr = test_addr + str(arg)
                    currentaddr.value = test_addr
                    currentstart.value = time.time()
                    test(result)
                    currentaddr.value = ''
                    resultQueue.put((ix, test_addr, test.tasks, batch(result)))
                except KeyboardInterrupt:
                    if len(currentaddr.value) > 0:
                        log.exception('Worker %d keyboard interrupt, failing current test %s',ix,test_addr)
                        currentaddr.value = ''
                        failure.Failure(*sys.exc_info())(result)
                        resultQueue.put((ix, test_addr, test.tasks, batch(result)))
                    else:
                        log.debug('test %s timed out',ix,test_addr)
                        result.addError(test,(TimedOutException,TimedOutException(test_addr),sys.exc_info()[2]))
                        resultQueue.put((ix, test_addr, test.tasks, batch(result)))
                except SystemExit:
                    currentaddr.value = ''
                    log.exception('Worker %d system exit',ix)
                    sys.stderr('Worker %d system exit\n'%ix)
                    raise
                except:
                    currentaddr.value = ''
                    log.exception("Worker %d error running test or returning results",ix)
                    failure.Failure(*sys.exc_info())(result)
                    resultQueue.put((ix, test_addr, test.tasks, batch(result)))
                if config.multiprocess_restartworker:
                    # this is necessary to prevent lockups if this thread adds more items to the queue?
                    # will data be corrupted?
                    #testQueue.cancel_join_thread()
                    #resultQueue.cancel_join_thread()
                    break
        except Empty:
            log.debug("Worker %s timed out waiting for tasks", ix)
    finally:
        testQueue.close()
        resultQueue.close()
    log.debug("Worker %s ending", ix)


class NoSharedFixtureContextSuite(ContextSuite):
    """
    Context suite that never fires shared fixtures.

    When a context sets _multiprocess_shared_, fixtures in that context
    are executed by the main process. Using this suite class prevents them
    from executing in the runner process as well.

    """
    testQueue = None
    tasks = None
    arg = None
    def setupContext(self, context):
        if getattr(context, '_multiprocess_shared_', False):
            return
        super(NoSharedFixtureContextSuite, self).setupContext(context)

    def teardownContext(self, context):
        if getattr(context, '_multiprocess_shared_', False):
            return
        super(NoSharedFixtureContextSuite, self).teardownContext(context)
    def run(self, result):
        """Run tests in suite inside of suite fixtures.
        """
        # proxy the result for myself
        log.debug("suite %s (%s) run called, tests: %s", id(self), self, self._tests)
        #import pdb
        #pdb.set_trace()
        if self.resultProxy:
            result, orig = self.resultProxy(result, self), result
        else:
            result, orig = result, result
        try:
            self.setUp()
        except KeyboardInterrupt:
            raise
        except:
            self.error_context = 'setup'
            result.addError(self, self._exc_info())
            return
        try:
            localtests = [test for test in self._tests]
            if len(localtests) > 1 and self.testQueue is not None:
                log.debug("queue %d tests"%len(localtests))
                for test in localtests:
                    if isinstance(test.test,nose.failure.Failure):
                        # proably failed in the generator, so execute directly to get the exception
                        test(orig)
                    else:
                        MultiProcessTestRunner.addtask(self.testQueue,self.tasks,test)
            else:
                #log.debug("execute %s"%str(localtests[0]))
                for test in localtests:
                    if isinstance(test,nose.case.Test) and self.arg is not None:
                        test.test.arg = self.arg
                    else:
                        test.arg = self.arg
                    test.testQueue = self.testQueue
                    test.tasks = self.tasks
                    if result.shouldStop:
                        log.debug("stopping")
                        break
                    # each nose.case.Test will create its own result proxy
                    # so the cases need the original result, to avoid proxy
                    # chains
                    test(orig)
        finally:
            self.has_run = True
            try:
                self.tearDown()
            except KeyboardInterrupt:
                raise
            except:
                self.error_context = 'teardown'
                result.addError(self, self._exc_info())
