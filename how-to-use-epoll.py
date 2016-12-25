#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 16-12-24 下午4:09
# @Author  : Sugare
# @mail    : 30733705@qq.com
# @Software: PyCharm

import socket, select       # select module包含epoll功能

EOL1 = b'\n\n'
EOL2 = b'\n\r\n'
response  = b'HTTP/1.0 200 OK\r\nDate: Mon, 1 Jan 1996 01:01:01 GMT\r\n'
response += b'Content-Type: text/plain\r\nContent-Length: 13\r\n\r\n'
response += b'Hello, world!'

serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # 在bind前让套接字允许地址重用，可将两个套接字绑定在一个端口上的意思，也就是说即使该端口已经别占用也可使用
serversocket.bind(('0.0.0.0', 8080))
serversocket.listen(1)
serversocket.setblocking(0)         # 由于套接字默认阻塞，这里使用非阻塞（异步）模式。

epoll = select.epoll()      # 创建一个epoll对象
epoll.register(serversocket.fileno(), select.EPOLLIN)   # 注册对服务器套接字上的读事件的钩子。只要服务器套接字接受套接字连接，就会发生读事件。
print 'serversocket:', serversocket.fileno()
try:
    connections = {}    # 存储fd:socketobject 键值对，将文件描述符（整数）映射到其相应的网络连接对象。
    requests = {}       # 存储fd:request 键值对，将文件描述符（整数）映射到其相应的客户端请求request。
    responses = {}      # 存储fd:response键值对，将文件描述符（整数）映射到其相应的回复客户端的response。
    while True:     # 进入ioloop
        events = epoll.poll(1)        # 等待事件的发生，查询epoll对象，了解是否发生了任何感兴趣的事件。参数“1”表示如果1s无链接到来，重新轮巡。如果在此轮巡时发生了任何感兴趣的事件，则查询将立即返回那些事件的列表。
        print 'evets', events         # 注意：该events并不只是监控上面serversocket上发生的事件，由于是死循环，还会监控下文所触发的事件
        for fileno, event in events:      # 事件作为（fileno，事件代码）元组的序列返回。 fileno是文件描述符的同义词，总是一个整数。由于上述监控的是服务器socket的fd，为了避免由于新链接（fd）的请求与客户端间的socket冲突，加入if控制
            if fileno == serversocket.fileno():        # 如果在套接字发生读取事件，则可能已创建新的套接字连接。
                connection, address = serversocket.accept()     # 当外来请求来访时，我们接受请求，建立链接，建立了一个新的通道也就是新的fd，注意fileno和下文connection.fileno的区别。
                connection.setblocking(0)       # 将新套接字设置为非阻塞模式。
                print 'conn', connection.fileno()       # 为了区别上述两个fileno，分别加入print语句
                epoll.register(connection.fileno(), select.EPOLLIN)     # 对套接字（connection.fileno）上的读事件的监控,换句话说就是如果request到来，通知相关进程（执行对应指令）
                connections[connection.fileno()] = connection       # connection为socket对象，将新建立的socketobject存入到上面的connections字典中
                requests[connection.fileno()] = b''             # 将‘’ 存入requests字典中，等数据触发时再赋值
                responses[connection.fileno()] = response       # 将上述的response存入上面的responses字典中
            elif event & select.EPOLLIN:
                requests[fileno] += connections[fileno].recv(1024)          # connections[fileno]其实是刚才存入的socketobject，相当于connection 如果发生读取事件，则读取从客户端发送来的新数据并将其存入到刚才的requests为‘’的字典中
                if EOL1 in requests[fileno] or EOL2 in requests[fileno]:       # 如果刚刚传来的请求中有两个空行，则说明请求已经成功收到。
                    epoll.modify(fileno, select.EPOLLOUT)            # 一旦收到完整的请求，则取消监控fd对读事件的触发，改写为监控写事件的触发。当发生写事件时候，将响应数据发送回客户端
                    print('-'*40 + '\n' + requests[fileno].decode()[:-2])       # 显示客户端发来的请求头信息
            elif event & select.EPOLLOUT:
                byteswritten = connections[fileno].send(responses[fileno])      # 每次一次发送响应数据，直到完整响应已传送到操作系统进行传输。
                responses[fileno] = responses[fileno][byteswritten:]
                if len(responses[fileno]) == 0:
                    epoll.modify(fileno, 0)          # 一旦发送完整的响应，就禁止对进一步读或写事件的兴趣。
                    connections[fileno].shutdown(socket.SHUT_RDWR)       # 如果明确关闭连接，则套接字关闭是可选的。此示例程序使用它，以便使客户端首先关闭。关闭调用通知客户端套接字不应发送或接收更多的数据，并将导致一个良好的客户端从它的末端关闭套接字连接。
            elif event & select.EPOLLHUP:          # HUP（挂断）事件指示客户端套接字已经断开连接（即关闭），因此该端也被关闭。没有必要注册对HUP事件的兴趣。它们总是在使用epoll对象注册的套接字上指示。
                epoll.unregister(fileno)        # 取消注册此套接字连接的兴趣
                connections[fileno].close()     # 关闭新链接的套接字
                del connections[fileno]

finally:
    epoll.unregister(serversocket.fileno())
    epoll.close()
    serversocket.close()

# This article from：http://scotdoyle.com/python-epoll-howto.html
# If you need to reprint please indicate the article from：https://github.com/sugare/tornado-source-analysis/how-to-use-epoll.py
   
