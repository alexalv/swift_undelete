#!/usr/bin/env python

import unittest
from swift.common import swob
from swift_undelete import middleware as md


class FakeApp(object):
    def __init__(self):
        self.responses = []  # fill in later
        self._calls = []

    def __call__(self, env, start_response):
        req = swob.Request(env)

        self._calls.append((
            req.method, req.path,
            # mutable dict; keep a copy so subsequent calls can't change it
            swob.HeaderKeyDict(req.headers)))

        if len(self.responses) > 1:
            resp = self.responses.pop(0)
        else:
            resp = self.responses[0]

        status = resp['status']
        headers = resp.get('headers', [])
        body_iter = resp.get('body_iter', [])
        start_response(status, headers)
        return body_iter

    @property
    def calls(self):
        """
        Returns the calls received by this application as a list of
        (method, path) pairs.
        """
        return [x[:2] for x in self._calls]

    @property
    def call_headers(self):
        """
        Returns the list of headers received by this application as it was
        called
        """
        return [x[2] for x in self._calls]

    @property
    def calls_with_headers(self):
        """
        Returns the calls received by this application as a list of
        (method, path, headers) tuples.
        """
        return self._calls


class TestConfigParsing(unittest.TestCase):
    def test_defaults(self):
        app = FakeApp()
        undelete = md.filter_factory({})(app)

        self.assertEqual(undelete.trash_prefix, ".trash-")
        self.assertEqual(undelete.trash_lifetime, 86400 * 90)

    def test_non_defaults(self):
        app = FakeApp()
        undelete = md.filter_factory({
            'trash_prefix': '.heap__',
            'trash_lifetime': '31536000'
        })(app)

        self.assertEqual(undelete.trash_prefix, ".heap__")
        self.assertEqual(undelete.trash_lifetime, 31536000)


class MiddlewareTestCase(unittest.TestCase):
    """
    Just a base class for other test cases. Some setup, some utility methods.
    Nothing too exciting.
    """
    def setUp(self):
        self.app = FakeApp()
        self.undelete = md.filter_factory({})(self.app)

    def call_mware(self, req, expect_exception=False):
        status = [None]
        headers = [None]

        def start_response(s, h, ei=None):
            status[0] = s
            headers[0] = h

        body_iter = self.undelete(req.environ, start_response)
        body = ''
        caught_exc = None
        try:
            for chunk in body_iter:
                body += chunk
        except Exception as exc:
            if expect_exception:
                caught_exc = exc
            else:
                raise

        headerdict = swob.HeaderKeyDict(headers[0])
        if expect_exception:
            return status[0], headerdict, body, caught_exc
        else:
            return status[0], headerdict, body


class TestPassthrough(MiddlewareTestCase):
    def test_account_passthrough(self):
        """
        Account requests are passed through unmodified.
        """
        self.app.responses = [{'status': '200 OK'}]

        req = swob.Request.blank('/v1/a')
        req.method = 'DELETE'

        status, _, _ = self.call_mware(req)
        self.assertEqual(status, "200 OK")
        self.assertEqual(self.app.calls, [('DELETE', '/v1/a')])

    def test_container_passthrough(self):
        """
        Container requests are passed through unmodified.
        """
        self.app.responses = [{'status': '200 OK'}]
        req = swob.Request.blank('/v1/a/c')
        req.method = 'DELETE'

        status, _, _ = self.call_mware(req)
        self.assertEqual(status, "200 OK")
        self.assertEqual(self.app.calls, [('DELETE', '/v1/a/c')])


class TestObjectDeletion(MiddlewareTestCase):
    def test_deleting_nonexistent_object(self):
        # If the object isn't there, ignore the 404 on COPY and pass the
        # DELETE request through. It might be an expired object, in which case
        # the object DELETE will actually get it out of the container listing
        # and free up some space.
        self.app.responses = [
            # COPY request
            {'status': '404 Not Found'},
            # trash-versions container creation request
            #
            # Ideally we'd skip this stuff, but we can't tell the difference
            # between object-not-found (404) and
            # destination-container-not-found (also 404).
            {'status': '202 Accepted'},
            # trash container creation request
            {'status': '202 Accepted'},
            # second COPY attempt:
            {'status': '404 Not Found'},
            # DELETE request
            {'status': '404 Not Found',
             'headers': [('X-Exophagous', 'ungrassed')]}]

        req = swob.Request.blank('/v1/a/elements/Cf')
        req.method = 'DELETE'

        status, headers, body = self.call_mware(req)
        self.assertEqual(status, "404 Not Found")
        self.assertEqual(headers.get('X-Exophagous'), 'ungrassed')
        self.assertEqual(self.app.calls,
                         [('COPY', '/v1/a/elements/Cf'),
                          ('PUT', '/v1/a/.trash-elements-versions'),
                          ('PUT', '/v1/a/.trash-elements'),
                          ('COPY', '/v1/a/elements/Cf'),
                          ('DELETE', '/v1/a/elements/Cf')])

    def test_copy_to_existing_trash_container(self):
        self.undelete.trash_lifetime = 1997339
        self.app.responses = [
            # COPY request
            {'status': '201 Created',
             'headers': [('X-Sir-Not-Appearing-In-This-Response', 'yup')]},
            # DELETE request
            {'status': '204 No Content',
             'headers': [('X-Decadation', 'coprose')]}]

        req = swob.Request.blank('/v1/MY_account/cats/kittens.jpg')
        req.method = 'DELETE'

        status, headers, body = self.call_mware(req)
        self.assertEqual(status, "204 No Content")
        # the client gets whatever the DELETE coughed up
        self.assertNotIn('X-Sir-Not-Appearing-In-This-Response', headers)
        self.assertEqual(headers['X-Decadation'], 'coprose')

        self.assertEqual(2, len(self.app.calls))

        # First, we performed a COPY request to save the object into the trash.
        method, path, headers = self.app.calls_with_headers[0]
        self.assertEqual(method, 'COPY')
        self.assertEqual(path, '/v1/MY_account/cats/kittens.jpg')
        self.assertEqual(headers['Destination'], '.trash-cats/kittens.jpg')
        self.assertEqual(headers['X-Delete-After'], str(1997339))

        # Second, we actually perform the DELETE request (and send that
        # response to the client unaltered)
        method, path, headers = self.app.calls_with_headers[1]
        self.assertEqual(method, 'DELETE')
        self.assertEqual(path, '/v1/MY_account/cats/kittens.jpg')

    def test_copy_to_existing_trash_container_no_expiration(self):
        self.undelete.trash_lifetime = 0
        self.app.responses = [
            # COPY request
            {'status': '201 Created'},
            # DELETE request
            {'status': '204 No Content'}]

        req = swob.Request.blank('/v1/MY_account/cats/kittens.jpg')
        req.method = 'DELETE'

        status, headers, body = self.call_mware(req)
        self.assertEqual(status, "204 No Content")
        self.assertEqual(2, len(self.app.calls))

        method, path, headers = self.app.calls_with_headers[0]
        self.assertEqual(method, 'COPY')
        self.assertEqual(path, '/v1/MY_account/cats/kittens.jpg')
        self.assertNotIn('X-Delete-After', headers)

    def test_copy_to_missing_trash_container(self):
        self.app.responses = [
            # first COPY attempt: trash container doesn't exist
            {'status': '404 Not Found'},
            # trash-versions container creation request
            {'status': '201 Created'},
            # trash container creation request
            {'status': '201 Created'},
            # second COPY attempt:
            {'status': '404 Not Found'},
            # DELETE request
            {'status': '204 No Content'}]

        req = swob.Request.blank('/v1/a/elements/Lv')
        req.method = 'DELETE'

        status, headers, body = self.call_mware(req)
        self.assertEqual(status, "204 No Content")
        self.assertEqual(self.app.calls,
                         [('COPY', '/v1/a/elements/Lv'),
                          ('PUT', '/v1/a/.trash-elements-versions'),
                          ('PUT', '/v1/a/.trash-elements'),
                          ('COPY', '/v1/a/elements/Lv'),
                          ('DELETE', '/v1/a/elements/Lv')])

    def test_copy_error(self):
        self.app.responses = [
            # COPY attempt: some mysterious error with some headers
            {'status': '503 Service Unavailable',
             'headers': [('X-Scraggedness', 'Goclenian')],
             'body_iter': ['dunno what happened boss']}]

        req = swob.Request.blank('/v1/a/elements/Te')
        req.method = 'DELETE'

        status, headers, body = self.call_mware(req)
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(headers.get('X-Scraggedness'), 'Goclenian')
        self.assertIn('what happened', body)
        self.assertEqual(self.app.calls, [('COPY', '/v1/a/elements/Te')])

    def test_copy_missing_trash_container_error_creating_vrs_container(self):
        self.app.responses = [
            # first COPY attempt: trash container doesn't exist
            {'status': '404 Not Found'},
            # trash-versions container creation request: failure!
            {'status': '403 Forbidden',
             'headers': [('X-Pupillidae', 'Barry')],
             'body_iter': ['oh hell no']}]

        req = swob.Request.blank('/v1/a/elements/U')
        req.method = 'DELETE'

        status, headers, body = self.call_mware(req)
        self.assertEqual(status, "403 Forbidden")
        self.assertEqual(headers.get('X-Pupillidae'), 'Barry')
        self.assertIn('oh hell no', body)
        self.assertEqual(self.app.calls,
                         [('COPY', '/v1/a/elements/U'),
                          ('PUT', '/v1/a/.trash-elements-versions')])

    def test_copy_missing_trash_container_error_creating_container(self):
        self.app.responses = [
            # first COPY attempt: trash container doesn't exist
            {'status': '404 Not Found'},
            # trash-versions container creation request
            {'status': '201 Created'},
            # trash container creation request: fails!
            {'status': "418 I'm a teapot",
             'headers': [('X-Body-Type', 'short and stout')],
             'body_iter': ['here is my handle, here is my spout']}]

        req = swob.Request.blank('/v1/a/elements/Mo')
        req.method = 'DELETE'

        status, headers, body = self.call_mware(req)
        self.assertEqual(status, "418 I'm a teapot")
        self.assertEqual(headers.get('X-Body-Type'), 'short and stout')
        self.assertIn('spout', body)
        self.assertEqual(self.app.calls,
                         [('COPY', '/v1/a/elements/Mo'),
                          ('PUT', '/v1/a/.trash-elements-versions'),
                          ('PUT', '/v1/a/.trash-elements')])

    def test_delete_from_trash(self):
        """
        Objects in trash containers don't get saved.
        """
        self.app.responses = [{'status': '204 No Content'}]

        req = swob.Request.blank('/v1/a/.trash-borkbork/bork')
        req.method = 'DELETE'

        status, headers, body = self.call_mware(req)
        self.assertEqual(status, "204 No Content")
        self.assertEqual(self.app.calls,
                         [('DELETE', '/v1/a/.trash-borkbork/bork')])
