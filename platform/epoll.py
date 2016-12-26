#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 16-12-24 下午4:09
# @Author  : Sugare
# @mail    : 30733705@qq.com

# Translated by Sugare
# If you need to reprint please indicate the article from：https://github.com/sugare/tornado-source-analysis/blob/master/platform/epoll.py

# Copyright 2012 Facebook
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
"""EPoll-based IOLoop implementation for Linux systems."""
from __future__ import absolute_import, division, print_function, with_statement

import select

from tornado.ioloop import PollIOLoop


class EPollIOLoop(PollIOLoop):
    """
    只是将select.epoll()驱动模型函数赋值给impl参数， 传递给PollIOLOOP中
    """
    def initialize(self, **kwargs):
        super(EPollIOLoop, self).initialize(impl=select.epoll(), **kwargs)
