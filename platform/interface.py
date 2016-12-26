#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 16-12-24 下午4:09
# @Author  : Sugare
# @mail    : 30733705@qq.com

# Translated by Sugare
# If you need to reprint please indicate the article from：https://github.com/sugare/tornado-source-analysis/blob/master/platform/interface.py
#
# Copyright 2011 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Interfaces for platform-specific functionality.

This module exists primarily for documentation purposes and as base classes
for other tornado.platform modules.  Most code should import the appropriate
implementation from `tornado.platform.auto`.
"""

from __future__ import absolute_import, division, print_function, with_statement


def set_close_exec(fd):
    """设置文件描述符的关闭执行位（“FD_CLOEXEC”）"""
    raise NotImplementedError()


class Waker(object):
    """一个类似socket的对象，可以从``select（）``唤醒另一个线程。

    `〜tornado.ioloop.IOLoop`将把Waker的`fileno（）`添加到``select``（或``epoll``或``kqueue``）调用中。
    当另一个线程想唤醒循环时，它调用`wake`。
    一旦它被唤醒，它将调用`consume`来执行任何必要的每次唤醒清除。
    当IOLoop关闭时候，waker也关闭。
    """
    def fileno(self):
        """返回此waker的读取文件描述符

        必须适合在本地平台上使用， 例如``select（）``或等效项。
        """
        raise NotImplementedError()

    def write_fileno(self):
        """返回此waker的写入文件描述符。"""
        raise NotImplementedError()

    def wake(self):
        """监听，触发事件后的进行调用"""
        raise NotImplementedError()

    def consume(self):
        """调用后，监听已经醒来的文件描述符，对其做任何必要的清理。"""
        raise NotImplementedError()

    def close(self):
        """关闭激活的文件描述符"""
        raise NotImplementedError()
