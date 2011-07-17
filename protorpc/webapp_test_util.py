#!/usr/bin/env python
#
# Copyright 2010 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Testing utilities for the webapp libraries.

  GetDefaultEnvironment: Method for easily setting up CGI environment.
  RequestHandlerTestBase: Base class for setting up handler tests.
"""

__author__ = 'rafek@google.com (Rafe Kaplan)'

import cStringIO
import threading
import unittest
import urllib2
from wsgiref import simple_server
from wsgiref import validate

from protorpc import protojson
from protorpc import remote
from protorpc import test_util
from protorpc import transport
from protorpc.webapp import service_handlers

from google.appengine.ext import webapp
from google.appengine.ext import testbed


class TestService(remote.Service):
  """Service used to do end to end tests with."""

  @remote.method(test_util.OptionalMessage,
                 test_util.OptionalMessage)
  def optional_message(self, request):
    if request.string_value:
      request.string_value = '+%s' % request.string_value
    return request


def GetDefaultEnvironment():
  """Function for creating a default CGI environment."""
  return {
    'LC_NUMERIC': 'C',
    'wsgi.multiprocess': True,
    'SERVER_PROTOCOL': 'HTTP/1.0',
    'SERVER_SOFTWARE': 'Dev AppServer 0.1',
    'SCRIPT_NAME': '',
    'LOGNAME': 'nickjohnson',
    'USER': 'nickjohnson',
    'QUERY_STRING': 'foo=bar&foo=baz&foo2=123',
    'PATH': '/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/bin/X11',
    'LANG': 'en_US',
    'LANGUAGE': 'en',
    'REMOTE_ADDR': '127.0.0.1',
    'LC_MONETARY': 'C',
    'CONTENT_TYPE': 'application/x-www-form-urlencoded',
    'wsgi.url_scheme': 'http',
    'SERVER_PORT': '8080',
    'HOME': '/home/mruser',
    'USERNAME': 'mruser',
    'CONTENT_LENGTH': '',
    'USER_IS_ADMIN': '1',
    'PYTHONPATH': '/tmp/setup',
    'LC_TIME': 'C',
    'HTTP_USER_AGENT': 'Mozilla/5.0 (X11; U; Linux i686 (x86_64); en-US; '
        'rv:1.8.1.6) Gecko/20070725 Firefox/2.0.0.6',
    'wsgi.multithread': False,
    'wsgi.version': (1, 0),
    'USER_EMAIL': 'test@example.com',
    'USER_EMAIL': '112',
    'wsgi.input': cStringIO.StringIO(),
    'PATH_TRANSLATED': '/tmp/request.py',
    'SERVER_NAME': 'localhost',
    'GATEWAY_INTERFACE': 'CGI/1.1',
    'wsgi.run_once': True,
    'LC_COLLATE': 'C',
    'HOSTNAME': 'myhost',
    'wsgi.errors': cStringIO.StringIO(),
    'PWD': '/tmp',
    'REQUEST_METHOD': 'GET',
    'MAIL': '/dev/null',
    'MAILCHECK': '0',
    'USER_NICKNAME': 'test',
    'HTTP_COOKIE': 'dev_appserver_login="test:test@example.com:True"',
    'PATH_INFO': '/tmp/myhandler'
  }


class RequestHandlerTestBase(test_util.TestCase):
  """Base class for writing RequestHandler tests.

  To test a specific request handler override CreateRequestHandler.
  To change the environment for that handler override GetEnvironment.
  """

  def setUp(self):
    """Set up test for request handler."""
    self.ResetHandler()

  def GetEnvironment(self):
    """Get environment.

    Override for more specific configurations.

    Returns:
      dict of CGI environment.
    """
    return GetDefaultEnvironment()

  def CreateRequestHandler(self):
    """Create RequestHandler instances.

    Override to create more specific kinds of RequestHandler instances.

    Returns:
      RequestHandler instance used in test.
    """
    return webapp.RequestHandler()

  def CheckResponse(self,
                    expected_status,
                    expected_headers,
                    expected_content):
    """Check that the web response is as expected.

    Args:
      expected_status: Expected status message.
      expected_headers: Dictionary of expected headers.  Will ignore unexpected
        headers and only check the value of those expected.
      expected_content: Expected body.
    """
    def check_content(content):
      self.assertEquals(expected_content, content)

    def start_response(status, headers):
      self.assertEquals(expected_status, status)

      found_keys = set()
      for name, value in headers:
        name = name.lower()
        try:
          expected_value = expected_headers[name]
        except KeyError:
          pass
        else:
          found_keys.add(name)
          self.assertEquals(expected_value, value)

      missing_headers = set(expected_headers.iterkeys()) - found_keys
      if missing_headers:
        self.fail('Expected keys %r not found' % (list(missing_headers),))

      return check_content

    self.handler.response.wsgi_write(start_response)

  def ResetHandler(self, change_environ=None):
    """Reset this tests environment with environment changes.

    Resets the entire test with a new handler which includes some changes to
    the default request environment.

    Args:
      change_environ: Dictionary of values that are added to default
        environment.
    """
    environment = self.GetEnvironment()
    environment.update(change_environ or {})

    self.request = webapp.Request(environment)
    self.response = webapp.Response()
    self.handler = self.CreateRequestHandler()
    self.handler.initialize(self.request, self.response)


class ServerThread(threading.Thread):
  """Thread responsible for managing wsgi server.

  This server does not just attach to the socket and listen for requests.  This
  is because the server classes in Python 2.5 or less have no way to shut them
  down.  Instead, the thread must be notified of how many requests it will
  receive so that it listens for each one individually.  Tests should tell how
  many requests to listen for using the handle_request method.
  """

  def __lock(method):
    """Decorator for methods that need to run in critical section."""
    def wrapper(self, *args, **kwargs):
      self.__condition.acquire()
      try:
        method(self, *args, **kwargs)
      finally:
        self.__condition.release()
    wrapper.__name__ = method.__name__
    return wrapper

  def __init__(self, server, *args, **kwargs):
    """Constructor.

    Args:
      server: The WSGI server that is served by this thread.
      As per threading.Thread base class.

    State:
      __condition: Condition used to lock access to thread state.
      __serving: Server is still expected to be serving.  When False server
        knows to shut itself down.
      __started: Thread has been started.  Necessary to prevent requests
        from coming to server before the WSGI server is ready to handle them.
      __requests: Number of requests that server should handle before waiting
        for additional notification.
    """
    self.server = server
    self.__condition = threading.Condition()
    self.__serving = True
    self.__started = False
    self.__requests = 0

    super(ServerThread, self).__init__(*args, **kwargs)

  @__lock
  def shutdown(self):
    """Notify server that it must shutdown gracefully."""
    self.__serving = False
    self.__condition.notify()

  @__lock
  def handle_request(self, request_count=1):
    """Notify the server that it must handle a number of incoming requests.

    Args:
      request_count: Number of requests to expect.
    """
    self.__requests += request_count
    self.__condition.notify()

  @__lock
  def wait_until_running(self):
    """Wait until the server thread is known to be running.

    This method should be called immediately after the threads "start" method
    has been called.  Without waiting it is possible for the parent thread to
    start notifying this thread about incoming requests before it is ready to
    receive them.
    """
    while not self.__started:
      self.__condition.wait()

  @__lock
  def run(self):
    """Handle incoming requests until shutdown."""
    self.__started = True
    self.__condition.notifyAll()
    while self.__serving:
      if self.__requests == 0:
        self.__condition.wait()
      else:
        self.server.handle_request()
        self.__requests -= 1

    self.server = None


class ServerTransportWrapper(transport.Transport):
  """Wrapper for a real transport that notifies server thread about requests.

  Since the server thread must receive notifications about requests that it must
  handle, it is helpful to have a transport wrapper that actually does this
  notification on each request.
  """

  def __init__(self, server_thread, transport):
    """Constructor.

    Args:
      server_thread: Instance of ServerThread to notify upon each request.
      transport: Actual transport that is being wrapped.
    """
    self.server_thread = server_thread
    self.transport = transport
    self.original_send_rpc = self.transport.send_rpc
    self.transport.send_rpc = self.send_rpc
    self._start_rpc = self.transport._start_rpc

  def send_rpc(self, *args, **kwargs):
    self.server_thread.handle_request()
    return self.original_send_rpc(*args, **kwargs)


class TestService(remote.Service):
  """Service used to do end to end tests with."""

  def __init__(self, message='uninitialized'):
    self.__message = message

  @remote.method(test_util.OptionalMessage, test_util.OptionalMessage)
  def optional_message(self, request):
    if request.string_value:
      request.string_value = '+%s' % request.string_value
    return request

  @remote.method(response_type=test_util.OptionalMessage)
  def init_parameter(self, request):
    return test_util.OptionalMessage(string_value=self.__message)

  @remote.method(test_util.NestedMessage, test_util.NestedMessage)
  def nested_message(self, request):
    request.string_value = '+%s' % requset.string_value
    return request

  @remote.method()
  def raise_application_error(self, request):
    raise remote.ApplicationError('This is an application error', 'ERROR_NAME')

  @remote.method()
  def raise_unexpected_error(self, request):
    raise TypeError('Unexpected error')

  @remote.method()
  def raise_rpc_error(self, request):
    raise remote.NetworkError('Uncaught network error')

  @remote.method(response_type=test_util.NestedMessage)
  def return_bad_message(self, request):
    return test_util.NestedMessage()


class AlternateService(remote.Service):
  """Service used to requesting non-existant methods."""

  @remote.method()
  def does_not_exist(self, request):
    raise NotImplementedError('Not implemented')


class WebServerTestBase(test_util.TestCase):

  USE_URLFETCH = False

  def setUp(self):
    self.port = test_util.pick_unused_port()
    self.server, self.application = self.StartWebServer(self.port)

    self.testbed = testbed.Testbed()
    self.testbed.activate()
    self.testbed.init_urlfetch_stub()

    if self.USE_URLFETCH:
      if transport.urlfetch is None:
        raise AssertionError('App Engine SDK is not in pythonpath')
    else:
      self._original_urlfetch = transport.urlfetch
      transport.urlfetch = None

    self.connection = ServerTransportWrapper(
      self.server,
      self.CreateTransport(self.service_url))

  def tearDown(self):
    self.server.shutdown()
    self.testbed.deactivate()

    if not self.USE_URLFETCH:
      transport.urlfetch = self._original_urlfetch

  def CreateTransport(self, service_url):
    """Create a new transportation object."""
    return transport.HttpTransport(service_url, protocol=protojson)

  def StartWebServer(self, port):
    """Start web server."""
    application = self.CreateWsgiApplication()
    validated_application = validate.validator(application)
    server = simple_server.make_server('localhost', port, validated_application)
    server = ServerThread(server)
    server.start()
    server.wait_until_running()
    return server, application

  @property
  def service_url(self):
    return 'http://localhost:%d/my/service' % self.port


class EndToEndTestBase(WebServerTestBase):

  # Sub-classes may override to create alternate configurations.
  DEFAULT_MAPPING = service_handlers.service_mapping(
    [('/my/service', TestService),
     ('/my/other_service', TestService.new_factory('initialized')),
    ])

  def setUp(self):
    super(EndToEndTestBase, self).setUp()

    self.stub = TestService.Stub(self.connection)

    self.other_connection = ServerTransportWrapper(
      self.server,
      self.CreateTransport(self.other_service_url))
    self.other_stub = TestService.Stub(self.other_connection)

    self.mismatched_stub = AlternateService.Stub(self.connection)

  @property
  def other_service_url(self):
    return 'http://localhost:%d/my/other_service' % self.port

  def CreateWsgiApplication(self):
    """Create WSGI application used on the server side for testing."""
    return webapp.WSGIApplication(self.DEFAULT_MAPPING, True)

  def DoRawRequest(self,
                   method,
                   content='',
                   content_type='application/json',
                   headers=None):
    headers = headers or {}
    headers.update({'content-length': len(content or ''),
                    'content-type': content_type,
                   })
    request = urllib2.Request('%s.%s' % (self.service_url, method),
                              content,
                              headers)
    self.server.handle_request()
    return urllib2.urlopen(request)

  def RawRequestError(self,
                      method,
                      content='',
                      content_type='application/json',
                      headers=None):
    try:
      self.DoRawRequest(method, content, content_type, headers)
      self.fail('Expected HTTP error')
    except urllib2.HTTPError, err:
      return err.code, err.read(), err.headers
