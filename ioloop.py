#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 16-12-24 下午4:09
# @Author  : Sugare
# @mail    : 30733705@qq.com

# Translated by Sugare
# If you need to reprint please indicate the article from：https://github.com/sugare/tornado-source-analysis/blob/master/ioloop.py


"""一个非阻塞IO事件循环.
大部分应用程序将在`IOLoop.instance`单实例中使用单个`IOLoop`对象。
`IOLoop.start`方法通常在main（）函数的结尾处调用。
少数部分应用程序使用多个IOLoop实例，例如每个线程一个IOLoop。
"""

from __future__ import absolute_import, division, print_function, with_statement

import datetime
import errno
import functools
import heapq
import itertools
import logging
import numbers
import os
import select
import sys
import threading
import time
import traceback
import math

from tornado.concurrent import TracebackFuture, is_future
from tornado.log import app_log, gen_log
from tornado import stack_context
from tornado.util import Configurable, errno_from_exception, timedelta_to_seconds

try:
    import signal
except ImportError:
    signal = None

try:
    import thread  # py2
except ImportError:
    import _thread as thread  # py3

from tornado.platform.auto import set_close_exec, Waker


_POLL_TIMEOUT = 3600.0


class TimeoutError(Exception):
    """
    定义一个时间超时异常
    """
    pass


class IOLoop(Configurable):
    """epoll的触发模式有两种：
    1. 水平触发：当监控的fd有事件发生时，程序会通知相关的程序去读写数据，如果没有相关程序来进行操作，或者数据未全部进行操作，则会一直通知。该IOLoop使用的是水平触发。
    2. 边缘触发：当监控的fd有事件发生时，程序会通知相关的程序去读写数据，只会通知一次，直到下次事件发生。

    如果所运行的系统是Linux则使用epoll模型，若是BSD或Mac则使用kqueue模型，
    如果二者都不是，则使用select模型。

    Example usage for a simple TCP server:

    .. testcode::

        import errno
        import functools
        import tornado.ioloop
        import socket

        def connection_ready(sock, fd, events):
            while True:
                try:
                    connection, address = sock.accept()
                except socket.error as e:
                    if e.args[0] not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    return
                connection.setblocking(0)
                handle_connection(connection, address)

        if __name__ == '__main__':
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setblocking(0)
            sock.bind(("", port))
            sock.listen(128)

            io_loop = tornado.ioloop.IOLoop.current()
            callback = functools.partial(connection_ready, sock)
            io_loop.add_handler(sock.fileno(), callback, io_loop.READ)
            io_loop.start()

    .. testoutput::
       :hide:
    默认情况下，如果之前没有IOLoop循环，则生成的IOLoop实例是当前线程的事件循环，
    如果传递参数‘make_current=True’则会使新构造的IOLoop成为当前的事件循环对象，如果之前已经存在IOLoop对象，则会抛出异常。
    """

    # epoll模块的常量
    _EPOLLIN = 0x001        # fd可用于阅读
    _EPOLLPRI = 0x002       # fd有紧急数据要读
    _EPOLLOUT = 0x004       # fd可用于写
    _EPOLLERR = 0x008       # fd上发生了错误
    _EPOLLHUP = 0x010       # 挂断fd
    _EPOLLRDHUP = 0x2000    # since kernel 2.6.17
    _EPOLLONESHOT = (1 << 30)       # 当fd上绑定的事件发生后，关闭fd
    _EPOLLET = (1 << 31)        # 设置为边缘触发，默认是水平触发模式

    # 自定义事件映射到epoll的相关事件，人性化
    NONE = 0
    READ = _EPOLLIN
    WRITE = _EPOLLOUT
    ERROR = _EPOLLERR | _EPOLLHUP

    # 用于创建全局IOLoop实例的全局锁
    # 线程加锁机制，防止多线程操作同一个fd，发生数据错乱。
    _instance_lock = threading.Lock()

    # 当前线程中实例化一个全局变量_current,threading.local()有如下特点：
    # 1. 子线程均可使用threading.local()的实例（_current）,如:子线程可设置任何_instance的变量，如_instance.local,_instance.key1...
    # 2. 当前线程生成的子线程，各自子线程中的变量独立，互不影响如:A线程的_instance.local 不同于 B线程中的_instance.local
    # 3. 若当前线程与子线程变量名字相同，如：主线程若也有_instance.local变量，不会覆盖当前线程
    _current = threading.local()


    # staticmethod和classmethod:
    # 1. @staticmethod或@classmethod，不需要实例化,所以二者都不需要第一个参数为self，直接类名.方法名()来调用。
    # 2. @staticmethod不需要表示自身对象的self和自身类的cls参数，就跟使用函数一样。但是要调用到这个类的一些属性方法，只能用类名.属性名/方法名。
    # 3. @classmethod第一个参数必须是自身类的cls参数。由于有cls函数，则调用类的属性方法可直接cls.method/attr,实例的属性方法需要cls().XXX
    @staticmethod
    def instance():
        """返回一个全局`IOLoop`实例。
        大多数应用程序都有一个单独的，全局的IOLoop运行在主线程上。
        使用此方法从另一个线程获取此实例。
        在大多数其他情况下，最好使用`current（）`来获取当前线程的IOLoop。
        """
        if not hasattr(IOLoop, "_instance"):
            with IOLoop._instance_lock:
                if not hasattr(IOLoop, "_instance"):
                    # New instance after double check
                    IOLoop._instance = IOLoop()
        return IOLoop._instance

    @staticmethod
    def initialized():
        """如果单例子IOLoop被创建，返回True"""
        return hasattr(IOLoop, "_instance")

    def install(self):
        """将此IOLoop对象安装为单例实例。

        这通常不是必要的，因为`instance（）`会创建一个'IOLoop'的需求，
        但你可能想调用`install`来使用IOLoop的自定义子类。
        当使用`IOLoop`子类时，必须在创建任何隐式创建自己的`IOLoop`对象之前调用`install`
        （例如：class：`tornado.httpclient.AsyncHTTPClient`）
        """
        assert not IOLoop.initialized()
        IOLoop._instance = self

    @staticmethod
    def clear_instance():
        """删除全局的IOLoop实例
        """
        if hasattr(IOLoop, "_instance"):
            del IOLoop._instance

    @staticmethod
    def current(instance=True):
        """返回当前线程的IOLoop.
        如果有一个IOLoop当前正在运行（通过查看IOLoop._current是否加入了instance属性），
        或者已经被make_current标记再当前运行，则返回实例。
        如果当前没有IOLoop，且instance参数为True, 返回IOLoop.instance(),即主线程的IOLoop

        一般来说，在构造一个异步对象时，你应该使用`IOLoop.current`作为默认值，
        当你想与不同的主线程通信时使用IOLoop.instance
        """
        current = getattr(IOLoop._current, "instance", None)    # 将IOLoop._current下的instance属性的值取出来，
        if current is None and instance:        # 如果没有值且instance=Ture
            return IOLoop.instance()
        return current

    def make_current(self):
        """使IOLoop实例存在于当前的线程
        当IOLoop启动时，它的线程自动变为当前线程，
        但有时在启动IOLoop之前显式调用`make_current`
        以便在启动时运行的代码可以找到正确的实例。
        """
        IOLoop._current.instance = self

    @staticmethod
    def clear_current():
        """清理当前的IOLoop实例
        """
        IOLoop._current.instance = None

    @classmethod
    def configurable_base(cls):
        return IOLoop

    @classmethod
    def configurable_default(cls):
        """此函数是根据不同的平台选择不同的事件驱动模型，
        从tornado.platform中导入事先定义好的模型，
        默认为SelectIOLoop
        """
        if hasattr(select, "epoll"):
            from tornado.platform.epoll import EPollIOLoop
            return EPollIOLoop
        if hasattr(select, "kqueue"):
            # Python 2.6+ on BSD or Mac
            from tornado.platform.kqueue import KQueueIOLoop
            return KQueueIOLoop
        from tornado.platform.select import SelectIOLoop
        return SelectIOLoop

    def initialize(self, make_current=None):
        if make_current is None:
            if IOLoop.current(instance=False) is None:
                self.make_current()
        elif make_current:
            if IOLoop.current(instance=False) is None:
                raise RuntimeError("current IOLoop already exists")
            self.make_current()

    def close(self, all_fds=False):
        """关闭所有的IOLoop实例，释放所有占用的资源。
        如果`all_fds` is true，所有再IOLoop上监控的文件描述符均关闭（不仅仅是由IOLoop本身创建的）

        许多应用程序将只使用单个'IOLoop'，该过程在整个生命周期中运行。
        在这种情况下，关闭IOLoop不是必要的，因为当进程退出时，一切都将被清除。
        `IOLoop.close'主要用于创建和销毁大量的“IOLoops”的场景，诸如单元测试。

        “IOLoop”必须完全停止才能关闭。
        当调用IOLoop.start()返回结果后，才可调用IOLoop.stop()
        因此，“close”的调用通常出现在调用'start'之后，而不是靠近对stop的调用。
        """
        raise NotImplementedError()

    def add_handler(self, fd, handler, events):
        """注册给定的处理程序以接收``fd``的给定事件。

        ``fd``参数可以是一个整数文件描述符或一个类似文件的对象，
        用``fileno（）``方法(或者是当`IOLoop`被关闭时可以被调用的`close（）`方法)

        ``events``参数是一个按位或常量的`IOLoop.READ``，``IOLoop.WRITE``和``IOLoop.ERROR``。

        当一个事件发生时，将会运行``handler（fd，events）``。

        """
        raise NotImplementedError()

    def update_handler(self, fd, events):
        """改变刚才注册的文件描述符 ``fd``.

        """
        raise NotImplementedError()

    def remove_handler(self, fd):
        """停止监空该文描述符上发生的事件``fd``.

        """
        raise NotImplementedError()

    def set_blocking_signal_threshold(self, seconds, action):
        """如果`IOLoop`被阻塞超过``seconds``秒，发送一个信号。

         ``seconds=None`` 禁用超时功能。

        action参数是一个Python信号处理程序。
        如果``action``为None，如果被阻塞的时间过长，进程将被杀死。
        """
        raise NotImplementedError()

    def set_blocking_log_threshold(self, seconds):
        """如果`IOLoop`被阻塞超过“seconds”秒，记录一个堆栈跟踪。

        相当于``set_blocking_signal_threshold（seconds，self.log_stack）``
        """
        self.set_blocking_signal_threshold(seconds, self.log_stack)

    def log_stack(self, signal, frame):
        """信号处理程序记录当前线程的堆栈跟踪。

        用于`set_blocking_signal_threshold`
        """
        gen_log.warning('IOLoop blocked for %f seconds in\n%s',
                        self._blocking_signal_threshold,
                        ''.join(traceback.format_stack(frame)))

    def start(self):
        """启动I / O循环

        循环将运行，直到其中一个回调调用`stop（）`，这将使循环在当前事件迭代完成后停止。
        """
        raise NotImplementedError()

    def _setup_logging(self):
        """IOLoop捕获和记录异常，所以重要的是日志输出是可见的。
        Python默认行为是打印一个无用的“没有处理程序可以找到”消息，而不是实际的日志条目，所以我们必须显式配置日志记录
        所以我们必须明确配置日志记录。

        这个方法应该从子类中的start（）调用。
        """
        if not any([logging.getLogger().handlers,
                    logging.getLogger('tornado').handlers,
                    logging.getLogger('tornado.application').handlers]):
            logging.basicConfig()

    def stop(self):
        """停止IO循环。

        如果事件循环当前未运行，则下一次调用`start（）`将立即返回。

        要使用异步方法从同步代码（如单元测试），你可以启动和停止事件循环如下::

          ioloop = IOLoop()
          async_method(ioloop=ioloop, callback=ioloop.stop)
          ioloop.start()

        ``ioloop.start（）``将在``async_method``运行其回调函数后返回，
        无论该回调在`ioloop.start`之前还是之后被调用。

        注意，即使`stop`被调用后，`IOLoop`也不会完全停止，直到IOLoop.start也返回。
        在调用'stop'之前安排的一些工作可能仍在'IOLoop'关闭之前运行。
        """
        raise NotImplementedError()

    def run_sync(self, func, timeout=None):
        """启动`IOLoop`，运行给定的函数，并停止循环。
        该函数必须返回一个可迭代对象或``None``。
        如果函数返回一个可迭代对象，IOLoop将运行直到迭代结束。
        如果它引发异常，IOLoop将停止，异常将重新提交给调用者。

        仅关键字参数“timeout”可用于设置函数的最大持续时间。如果超时到期，会引发一个`TimeoutError`。

        这个方法与`tornado.gen.coroutine`结合使用，允许在``main（）``function ::

            @gen.coroutine
            def main():
                # do stuff...

            if __name__ == '__main__':
                IOLoop.current().run_sync(main)
        """
        future_cell = [None]

        def run():
            try:
                result = func()
            except Exception:
                future_cell[0] = TracebackFuture()
                future_cell[0].set_exc_info(sys.exc_info())
            else:
                if is_future(result):
                    future_cell[0] = result
                else:
                    future_cell[0] = TracebackFuture()
                    future_cell[0].set_result(result)
            self.add_future(future_cell[0], lambda future: self.stop())
        self.add_callback(run)
        if timeout is not None:
            timeout_handle = self.add_timeout(self.time() + timeout, self.stop)
        self.start()
        if timeout is not None:
            self.remove_timeout(timeout_handle)
        if not future_cell[0].done():
            raise TimeoutError('Operation timed out after %s seconds' % timeout)
        return future_cell[0].result()

    def time(self):
        """根据IOLoop的时钟返回当前时间。

        返回值是相对于过去的未指定时间的浮点数。
        默认情况下，IOLoop的时间函数是'time.time'。然而，其可以被配置为使用`time.monotonic`。
        调用`add_timeout`传递一个数字而不是`datetime.timedelta`应该使用这个函数来计算适当的时间，
        所以无论什么时候功能被选择，他们都可以工作。

        """
        return time.time()

    def add_timeout(self, deadline, callback, *args, **kwargs):
        """在I / O循环的“deadline”时间运行``callback``。

        返回一个不透明的句柄，可以传递给`remove_timeout`来取消。

        ``deadline``可以是一个表示时间的数字（与'IOLoop.time`，通常是'time.time`相同的大小），
        或者相对于当前时间的截止日期的`datetime.timedelta`对象。

        注意，从其他线程调用`add_timeout`是不安全的。
        相反，你必须使用`add_callback`将控制转移到`IOLoop`的线程，然后从那里调用`add_timeout`。

        IOLoop的子类必须实现`add_timeout`或`call_at`;每个的默认实现将调用另一个。
        `call_at`通常更容易实现，但希望保持与4.0之前的Tornado版本兼容性的子类必须使用`add_timeout`

        """
        if isinstance(deadline, numbers.Real):
            return self.call_at(deadline, callback, *args, **kwargs)
        elif isinstance(deadline, datetime.timedelta):
            return self.call_at(self.time() + timedelta_to_seconds(deadline),
                                callback, *args, **kwargs)
        else:
            raise TypeError("Unsupported deadline %r" % deadline)

    def call_later(self, delay, callback, *args, **kwargs):
        """在“延迟”秒钟过后运行``callback``。

        返回一个不透明的句柄，可以传递给`remove_timeout`来取消。
        注意，与同名的`asyncio`方法不同，返回的对象没有``cancel（）``方法。
        """
        return self.call_at(self.time() + delay, callback, *args, **kwargs)

    def call_at(self, when, callback, *args, **kwargs):
        """在``when``指定的绝对时间运行``callback``。

        ``when``必须是使用与IOLoop.time相同参考点的数字。

        返回一个不透明的句柄，可以传递给`remove_timeout`来取消。
        注意，与同名的`asyncio`方法不同，返回的对象没有``cancel（）``方法。
        """
        return self.add_timeout(when, callback, *args, **kwargs)

    def remove_timeout(self, timeout):
        """取消待处理的超时

        参数是由`add_timeout`返回的句柄。即使回调已经运行，调用`remove_timeout`也是安全的。
        """
        raise NotImplementedError()

    def add_callback(self, callback, *args, **kwargs):
        """在下一个I / O循环迭代中调用给定的回调。
        从任何线程调用此方法是安全的，除了从信号处理程序。
        注意，这是`IOLoop`中的** only **方法，这使得这个线程安全保证;
        所有其他与IOLoop的交互必须从I​​OLoop的线程中完成。
        `add_callback（）`可以用于将控制从其他线程传递到IOLoop的线程。
        """
        raise NotImplementedError()

    def add_callback_from_signal(self, callback, *args, **kwargs):
        """在下一个I / O循环迭代中调用给定的回调。

        安全使用从Python信号处理程序
        为了避免拾取由中断的函数的上下文，使用此方法添加的回调将在没有任何`.stack_context`的情况下运行。

        """
        raise NotImplementedError()

    def spawn_callback(self, callback, *args, **kwargs):
        """在下一个IOLoop迭代上调用给定的回调。

        与IOLoop上的所有其他回调相关的方法不同，
        `spawn_callback``不会将回调与其调用者的“stack_context”关联，
        因此它适用于不应干扰调用者的fire-and-forget回调。
        """
        with stack_context.NullContext():
            self.add_callback(callback, *args, **kwargs)

    def add_future(self, future, callback):
        """当给定的`future`完成时，在`IOLoop``上调度一个回调。

        回调被调用一个参数，`.Future`。
        """
        assert is_future(future)
        callback = stack_context.wrap(callback)
        future.add_done_callback(
            lambda future: self.add_callback(callback, future))

    def _run_callback(self, callback):
        """运行带错误处理的回调。
        用于子类。
        """
        try:
            ret = callback()
            if ret is not None and is_future(ret):
                # Functions that return Futures typically swallow all
                # exceptions and store them in the Future.  If a Future
                # makes it out to the IOLoop, ensure its exception (if any)
                # gets logged too.
                self.add_future(ret, lambda f: f.result())
        except Exception:
            self.handle_callback_exception(callback)

    def handle_callback_exception(self, callback):
        """每当由IOLoop运行的回调抛出异常时，将调用此方法。

        默认情况下，只是将异常记录为错误。子类可以覆盖此方法以自定义异常报告。

        异常本身没有明确传递，但在`sys.exc_info`中可用。
        """
        app_log.error("Exception in callback %r", callback, exc_info=True)

    def split_fd(self, fd):
        """从``fd``参数返回（fd，obj）对。

        我们接受原始文件描述符和类文件对象作为`add_handler`和相关方法的输入。
        当一个类文件对象被传递时，我们必须保留对象本身，所以我们可以在IOLoop关闭时正确关闭它，
        但poller接口支持文件描述符（它们将接受类似文件的对象并为你调用`fileno（）``，但它们总是返回描述符自己

        这个方法提供给IOLoop的子类使用，通常不应该被应用程序代码使用。
        """
        try:
            return fd.fileno(), fd
        except AttributeError:
            return fd, fd

    def close_fd(self, fd):
        """关闭``fd``的实用方法。

        如果``fd``是一个类文件对象，我们直接关闭它;否则我们使用`os.close`。

        这个方法提供给`IOLoop`子类使用（在`IOLoop.close（all_fds = True）``的实现中，
        并且一般不应该由应用程序代码使用。
        """
        try:
            try:
                fd.close()
            except AttributeError:
                os.close(fd)
        except OSError:
            pass


class PollIOLoop(IOLoop):
    """基于类选择函数构建的IOLoops的基类。
    """
    def initialize(self, impl, time_func=None, **kwargs):
        super(PollIOLoop, self).initialize(**kwargs)
        self._impl = impl
        if hasattr(self._impl, 'fileno'):
            set_close_exec(self._impl.fileno())
        self.time_func = time_func or time.time
        self._handlers = {}
        self._events = {}
        self._callbacks = []
        self._callback_lock = threading.Lock()
        self._timeouts = []
        self._cancellations = 0
        self._running = False
        self._stopped = False
        self._closing = False
        self._thread_ident = None
        self._blocking_signal_threshold = None
        self._timeout_counter = itertools.count()

        # 创建一个管道，当我们想要唤醒I / O循环时，它空闲时发送伪造数据
        self._waker = Waker()

        # self._waker.consume()函数实现了无线循环监听，若有事件触发，读取内容
        self.add_handler(self._waker.fileno(),
                         lambda fd, events: self._waker.consume(),
                         self.READ)

    def close(self, all_fds=False):
        """
        all_fds=True：关闭所有的之前监控的文件描述符
        :param all_fds:
        :return:
        """
        with self._callback_lock:
            self._closing = True
        self.remove_handler(self._waker.fileno())
        if all_fds:
            for fd, handler in self._handlers.values():
                self.close_fd(fd)
        self._waker.close()
        self._impl.close()
        self._callbacks = None
        self._timeouts = None

    def add_handler(self, fd, handler, events):
        """
        对套接字fd上的事件监控,换句话说就是如果触发events，通知相关进程（执行对应指令）
        """
        fd, obj = self.split_fd(fd)
        self._handlers[fd] = (obj, stack_context.wrap(handler))
        self._impl.register(fd, events | self.ERROR)

    def update_handler(self, fd, events):
        """
        更改套接字上监控的事件
        """
        fd, obj = self.split_fd(fd)
        self._impl.modify(fd, events | self.ERROR)

    def remove_handler(self, fd):
        """
        丢弃对fd的事件监控，清楚_headlers中的(fd:obj)键值对
        以及清除再_events中的对应的fd的键值对
        """
        fd, obj = self.split_fd(fd)
        self._handlers.pop(fd, None)
        self._events.pop(fd, None)
        try:
            self._impl.unregister(fd)
        except Exception:
            gen_log.debug("Error deleting fd from IOLoop", exc_info=True)

    def set_blocking_signal_threshold(self, seconds, action):
        if not hasattr(signal, "setitimer"):
            gen_log.error("set_blocking_signal_threshold requires a signal module "
                          "with the setitimer method")
            return
        self._blocking_signal_threshold = seconds
        if seconds is not None:
            signal.signal(signal.SIGALRM,
                          action if action is not None else signal.SIG_DFL)

    def start(self):
        if self._running:
            raise RuntimeError("IOLoop is already running")
        self._setup_logging()
        if self._stopped:
            self._stopped = False
            return
        old_current = getattr(IOLoop._current, "instance", None)
        IOLoop._current.instance = self
        self._thread_ident = thread.get_ident()     # 返回当前线程的'线程标识符',线程标识符可以在线程退出并创建另一个线程时被回收
        self._running = True

        # signal.set_wakeup_fd关闭事件循环中的竞争条件：
        # 信号可以在它进入其可中断睡眠之前到达select / poll /等的开始，因此信号将在不唤醒的情况下被消耗
        # 解决方案是将（C，同步）信号处理程序写入管道，然后将被选择看到。
        # 在python的信号处理语义中，这只在主线程上有意义（幸运的是，set_wakeup_fd只在主线程上工作，否则会引发ValueError')
        # 如果有人已经设置了唤醒fd，我们不想干扰它。
        # 这是一个扭曲的问题，它的＃SIGCHLD处理响应自己的唤醒fd被写入。
        # 只要唤醒fd在IOLoop上注册，循环仍然会醒来，一切都应该工作。

        old_wakeup_fd = None
        if hasattr(signal, 'set_wakeup_fd') and os.name == 'posix':
            # requires python 2.6+, unix.  set_wakeup_fd exists but crashes
            # the python process on windows.
            try:
                old_wakeup_fd = signal.set_wakeup_fd(self._waker.write_fileno())
                if old_wakeup_fd != -1:
                    # Already set, restore previous value.  This is a little racy,
                    # but there's no clean get_wakeup_fd and in real use the
                    # IOLoop is just started once at the beginning.
                    signal.set_wakeup_fd(old_wakeup_fd)
                    old_wakeup_fd = None
            except ValueError:
                # Non-main thread, or the previous value of wakeup_fd
                # is no longer valid.
                old_wakeup_fd = None

        try:
            while True:
                # 通过将新的回调延迟到事件循环的下一次迭代来防止IO事件饥饿。
                with self._callback_lock:
                    callbacks = self._callbacks
                    self._callbacks = []

                # Add any timeouts that have come due to the callback list.
                # Do not run anything until we have determined which ones
                # are ready, so timeouts that call add_timeout cannot
                # schedule anything in this iteration.
                due_timeouts = []
                if self._timeouts:
                    now = self.time()
                    while self._timeouts:
                        if self._timeouts[0].callback is None:
                            # The timeout was cancelled.  Note that the
                            # cancellation check is repeated below for timeouts
                            # that are cancelled by another timeout or callback.
                            heapq.heappop(self._timeouts)
                            self._cancellations -= 1
                        elif self._timeouts[0].deadline <= now:
                            due_timeouts.append(heapq.heappop(self._timeouts))
                        else:
                            break
                    if (self._cancellations > 512
                            and self._cancellations > (len(self._timeouts) >> 1)):
                        # Clean up the timeout queue when it gets large and it's
                        # more than half cancellations.
                        self._cancellations = 0
                        self._timeouts = [x for x in self._timeouts
                                          if x.callback is not None]
                        heapq.heapify(self._timeouts)

                for callback in callbacks:
                    self._run_callback(callback)
                for timeout in due_timeouts:
                    if timeout.callback is not None:
                        self._run_callback(timeout.callback)
                # Closures may be holding on to a lot of memory, so allow
                # them to be freed before we go into our poll wait.
                callbacks = callback = due_timeouts = timeout = None

                if self._callbacks:
                    # If any callbacks or timeouts called add_callback,
                    # we don't want to wait in poll() before we run them.
                    poll_timeout = 0.0
                elif self._timeouts:
                    # If there are any timeouts, schedule the first one.
                    # Use self.time() instead of 'now' to account for time
                    # spent running callbacks.
                    poll_timeout = self._timeouts[0].deadline - self.time()
                    poll_timeout = max(0, min(poll_timeout, _POLL_TIMEOUT))
                else:
                    # No timeouts and no callbacks, so use the default.
                    poll_timeout = _POLL_TIMEOUT

                if not self._running:
                    break

                if self._blocking_signal_threshold is not None:
                    # clear alarm so it doesn't fire while poll is waiting for
                    # events.
                    signal.setitimer(signal.ITIMER_REAL, 0, 0)

                try:
                    event_pairs = self._impl.poll(poll_timeout)
                except Exception as e:
                    # Depending on python version and IOLoop implementation,
                    # different exception types may be thrown and there are
                    # two ways EINTR might be signaled:
                    # * e.errno == errno.EINTR
                    # * e.args is like (errno.EINTR, 'Interrupted system call')
                    if errno_from_exception(e) == errno.EINTR:
                        continue
                    else:
                        raise

                if self._blocking_signal_threshold is not None:
                    signal.setitimer(signal.ITIMER_REAL,
                                     self._blocking_signal_threshold, 0)

                # Pop one fd at a time from the set of pending fds and run
                # its handler. Since that handler may perform actions on
                # other file descriptors, there may be reentrant calls to
                # this IOLoop that update self._events
                self._events.update(event_pairs)
                while self._events:
                    fd, events = self._events.popitem()
                    try:
                        fd_obj, handler_func = self._handlers[fd]
                        handler_func(fd_obj, events)
                    except (OSError, IOError) as e:
                        if errno_from_exception(e) == errno.EPIPE:
                            # Happens when the client closes the connection
                            pass
                        else:
                            self.handle_callback_exception(self._handlers.get(fd))
                    except Exception:
                        self.handle_callback_exception(self._handlers.get(fd))
                fd_obj = handler_func = None

        finally:
            # reset the stopped flag so another start/stop pair can be issued
            self._stopped = False
            if self._blocking_signal_threshold is not None:
                signal.setitimer(signal.ITIMER_REAL, 0, 0)
            IOLoop._current.instance = old_current
            if old_wakeup_fd is not None:
                signal.set_wakeup_fd(old_wakeup_fd)

    def stop(self):
        self._running = False
        self._stopped = True
        self._waker.wake()

    def time(self):
        return self.time_func()

    def call_at(self, deadline, callback, *args, **kwargs):
        timeout = _Timeout(
            deadline,
            functools.partial(stack_context.wrap(callback), *args, **kwargs),
            self)
        heapq.heappush(self._timeouts, timeout)
        return timeout

    def remove_timeout(self, timeout):
        # Removing from a heap is complicated, so just leave the defunct
        # timeout object in the queue (see discussion in
        # http://docs.python.org/library/heapq.html).
        # If this turns out to be a problem, we could add a garbage
        # collection pass whenever there are too many dead timeouts.
        timeout.callback = None
        self._cancellations += 1

    def add_callback(self, callback, *args, **kwargs):
        with self._callback_lock:
            if self._closing:
                raise RuntimeError("IOLoop is closing")
            list_empty = not self._callbacks
            self._callbacks.append(functools.partial(
                stack_context.wrap(callback), *args, **kwargs))
            if list_empty and thread.get_ident() != self._thread_ident:
                # If we're in the IOLoop's thread, we know it's not currently
                # polling.  If we're not, and we added the first callback to an
                # empty list, we may need to wake it up (it may wake up on its
                # own, but an occasional extra wake is harmless).  Waking
                # up a polling IOLoop is relatively expensive, so we try to
                # avoid it when we can.
                self._waker.wake()

    def add_callback_from_signal(self, callback, *args, **kwargs):
        with stack_context.NullContext():
            if thread.get_ident() != self._thread_ident:
                # if the signal is handled on another thread, we can add
                # it normally (modulo the NullContext)
                self.add_callback(callback, *args, **kwargs)
            else:
                # If we're on the IOLoop's thread, we cannot use
                # the regular add_callback because it may deadlock on
                # _callback_lock.  Blindly insert into self._callbacks.
                # This is safe because the GIL makes list.append atomic.
                # One subtlety is that if the signal interrupted the
                # _callback_lock block in IOLoop.start, we may modify
                # either the old or new version of self._callbacks,
                # but either way will work.
                self._callbacks.append(functools.partial(
                    stack_context.wrap(callback), *args, **kwargs))


class _Timeout(object):
    """IOLoop超时，UNIX时间戳和回调"""

    # 当有大量待处理的回调时，减少内存开销
    __slots__ = ['deadline', 'callback', 'tiebreaker']

    def __init__(self, deadline, callback, io_loop):
        if not isinstance(deadline, numbers.Real):
            raise TypeError("Unsupported deadline %r" % deadline)
        self.deadline = deadline
        self.callback = callback
        self.tiebreaker = next(io_loop._timeout_counter)

    # Comparison methods to sort by deadline, with object id as a tiebreaker
    # to guarantee a consistent ordering.  The heapq module uses __le__
    # in python2.5, and __lt__ in 2.6+ (sort() and most other comparisons
    # use __lt__).
    def __lt__(self, other):
        return ((self.deadline, self.tiebreaker) <
                (other.deadline, other.tiebreaker))

    def __le__(self, other):
        return ((self.deadline, self.tiebreaker) <=
                (other.deadline, other.tiebreaker))


class PeriodicCallback(object):
    """调度要定期调用的给定回调。

    回调被每个``callback_time``毫秒调用。
    请注意，超时以毫秒为单位，而tornado中的大多数其他与时间相关的函数使用秒。

    如果回调运行的时间比``callback_time``毫秒更长，则后续调用将被跳过以按计划返回。

    `start`必须在创建“PeriodicCallback”后调用。
    """
    def __init__(self, callback, callback_time, io_loop=None):
        self.callback = callback
        if callback_time <= 0:
            raise ValueError("Periodic callback must have a positive callback_time")
        self.callback_time = callback_time
        self.io_loop = io_loop or IOLoop.current()
        self._running = False
        self._timeout = None

    def start(self):
        """Starts the timer."""
        self._running = True
        self._next_timeout = self.io_loop.time()
        self._schedule_next()

    def stop(self):
        """Stops the timer."""
        self._running = False
        if self._timeout is not None:
            self.io_loop.remove_timeout(self._timeout)
            self._timeout = None

    def is_running(self):
        """Return True if this `.PeriodicCallback` has been started.

        .. versionadded:: 4.1
        """
        return self._running

    def _run(self):
        if not self._running:
            return
        try:
            return self.callback()
        except Exception:
            self.io_loop.handle_callback_exception(self.callback)
        finally:
            self._schedule_next()

    def _schedule_next(self):
        if self._running:
            current_time = self.io_loop.time()

            if self._next_timeout <= current_time:
                callback_time_sec = self.callback_time / 1000.0
                self._next_timeout += (math.floor((current_time - self._next_timeout) / callback_time_sec) + 1) * callback_time_sec

            self._timeout = self.io_loop.add_timeout(self._next_timeout, self._run)
