# Copyright (c) 2014 SwiftStack, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Middleware for OpenStack Swift that implements undelete functionality.

When this middleware is installed, an object DELETE request will cause a copy
of the object to be saved into a "trash location" prior to deletion.
Subsequently, an administrator can recover the deleted object.

Caveats:

 * This does not provide protection against overwriting an object. Use Swift's
   object versioning if you require overwrite protection.

 * If your account names are near the maximum length, this middleware will
   fail to create trash accounts, leaving some objects unable to be deleted.

 * If your container names are near the maximum length, this middleware will
   fail to create trash containers, leaving some objects unable to be deleted.

 * If your cluster is too full to allow an object to be copied, you will be
   unable to delete it. In extremely full clusters, this may result in a
   situation where you need to add capacity before you can delete objects.

Future work:

 * Allow undelete to be enabled only for particular accounts or containers

 * Move to separate account, not container, for trash. This requires Swift to
   allow cross-account COPY requests.

 * If block_trash_deletes is on, modify the Allow header in responses (both
   OPTIONS responses and any other 405 response).

"""
from swift.common import http, swob, utils, wsgi

DEFAULT_TRASH_PREFIX = ".trash-"
DEFAULT_TRASH_LIFETIME = 86400 * 90  # 90 days expressed in seconds


# Helper method stolen from a pending Swift change in Gerrit.
#
# If it ever actually lands, import and use it instead of having this
# duplication.
def close_if_possible(maybe_closable):
    close_method = getattr(maybe_closable, 'close', None)
    if callable(close_method):
        return close_method()


def friendly_error(orig_error):
    return "Error copying object to trash:\n" + orig_error


class ContainerContext(wsgi.WSGIContext):
    """
    Helper class to perform a container PUT request.
    """

    def create(self, env, vrs, account, container, versions=None):
        """
        Perform a container PUT request

        :param env: WSGI environment for original request
        :param vrs: API version, e.g. "v1"
        :param account: account in which to create the container
        :param container: container name
        :param versions: value for X-Versions-Location header
            (for container versioning)

        :returns: None
        :raises: HTTPException on failure (non-2xx response)
        """
        env = env.copy()
        env['REQUEST_METHOD'] = 'PUT'
        env["PATH_INFO"] = "/%s/%s/%s" % (vrs, account, container)
        if versions:
            env['HTTP_X_VERSIONS_LOCATION'] = versions

        resp_iter = self._app_call(env)
        # The body of a PUT response is either empty or very short (e.g. error
        # message), so we can get away with slurping the whole thing.
        body = ''.join(resp_iter)
        close_if_possible(resp_iter)

        status_int = int(self._response_status.split(' ', 1)[0])
        if not http.is_success(status_int):
            raise swob.HTTPException(
                status=self._response_status,
                headers=self._response_headers,
                body=friendly_error(body))


class CopyContext(wsgi.WSGIContext):
    """
    Helper class to perform an object COPY request.
    """

    def copy(self, env, destination_container, destination_object,
             delete_after=None):
        """
        Perform a COPY from source to destination.

        :param env: WSGI environment for a request aimed at the source
            object.
        :param destination_container: container to copy into.
            Note: this must not contain any slashes or the request is
            guaranteed to fail.
        :param destination_object: destination object name
        :param delete_after: value of X-Delete-After; object will be deleted
                             after that many seconds have elapsed. Set to 0 or
                             None to keep the object forever.

        :returns: 3-tuple (HTTP status code, response headers,
                           full response body)
        """
        env = env.copy()
        env['REQUEST_METHOD'] = 'COPY'
        env['HTTP_DESTINATION'] = '/'.join(
            (destination_container, destination_object))
        qs = env.get('QUERY_STRING', '')
        if qs:
            qs += '&multipart-manifest=get'
        else:
            qs = 'multipart-manifest=get'
        env['QUERY_STRING'] = qs
        if delete_after:
            env['HTTP_X_DELETE_AFTER'] = str(delete_after)
        resp_iter = self._app_call(env)
        # The body of a COPY response is either empty or very short (e.g.
        # error message), so we can get away with slurping the whole thing.
        body = ''.join(resp_iter)
        close_if_possible(resp_iter)

        status_int = int(self._response_status.split(' ', 1)[0])
        return (status_int, self._response_headers, body)


class HeadContext(wsgi.WSGIContext):

    def headers(self, env, vrs, acc, con):
        """
        Determine whether or not we should process this container
        """
        env = env.copy()
        env['REQUEST_METHOD'] = 'HEAD'
        env['HTTP_DESTINATION'] = '/'.join(
            (vrs,acc,con))
        resp_iter = self._app_call(env)
        body = ''.join(resp_iter)
        close_if_possible(resp_iter)
        status_int = int(self._response_status.split(' ', 1)[0])
        return self._response_headers

class UndeleteMiddleware(object):
    def __init__(self, app, trash_prefix=DEFAULT_TRASH_PREFIX,
                 trash_lifetime=DEFAULT_TRASH_LIFETIME,
                 block_trash_deletes=False):
        self.app = app
        self.trash_prefix = trash_prefix
        self.trash_lifetime = trash_lifetime
        self.block_trash_deletes = block_trash_deletes

    @swob.wsgify
    def __call__(self, req):
        # We only want to step in on object DELETE requests
        if req.method != 'DELETE':
            return self.app
        try:
            vrs, acc, con, obj = req.split_path(4, 4, rest_with_last=True)
        except ValueError:
            # not an object request
            return self.app

        #print req.environ
        print HeadContext(self.app).headers(req.environ,vrs,acc,con)

        # Okay, this is definitely an object DELETE request; let's see if it's
        # one we want to step in for.
        if self.is_trash(con) and self.block_trash_deletes:
            return swob.HTTPMethodNotAllowed(
                content_type="text/plain",
                body=("Attempted to delete from a trash container, but "
                      "block_trash_deletes is enabled\n"))
        elif not self.should_save_copy(req.environ, con, obj):
            return self.app

        trash_container = self.trash_prefix + con
        copy_status, copy_headers, copy_body = self.copy_object(
            req, trash_container, obj)
        if copy_status == 404:
            self.create_trash_container(req, vrs, acc, trash_container)
            copy_status, copy_headers, copy_body = self.copy_object(
                req, trash_container, obj)
        elif not http.is_success(copy_status):
            # other error; propagate this to the client
            return swob.Response(
                body=friendly_error(copy_body),
                status=copy_status,
                headers=copy_headers)
        return self.app

    def copy_object(self, req, trash_container, obj):
        return CopyContext(self.app).copy(req.environ, trash_container, obj,
                                          self.trash_lifetime)

    def create_trash_container(self, req, vrs, account, trash_container):
        """
        Create a trash container and its associated versions container.

        :raises HTTPException: if container creation failed
        """
        ctx = ContainerContext(self.app)
        versions_container = trash_container + "-versions"
        ctx.create(req.environ, vrs, account, versions_container)
        ctx.create(req.environ, vrs, account, trash_container,
                   versions=versions_container)

    def is_trash(self, con):
        """
        Whether a container is a trash container or not
        """
        return con.startswith(self.trash_prefix)


    def should_save_copy(self, env, con, obj):
        """
        Determine whether or not we should save a copy of the object prior to
        its deletion. For example, if the object is one that's in a trash
        container, don't save a copy lest we get infinite metatrash recursion.
        """
        return not self.is_trash(con)


def filter_factory(global_conf, **local_conf):
    """
    Returns the WSGI filter for use with paste.deploy.

    Parameters in config:

    # value to prepend to the account in order to compute the trash location
    trash_prefix = ".trash-"
    # how long, in seconds, trash objects should live before expiring. Set to 0
    # to keep trash objects forever.
    trash_lifetime = 7776000  # 90 days
    """
    conf = global_conf.copy()
    conf.update(local_conf)

    trash_prefix = conf.get("trash_prefix", DEFAULT_TRASH_PREFIX)
    trash_lifetime = int(conf.get("trash_lifetime", DEFAULT_TRASH_LIFETIME))
    block_trash_deletes = utils.config_true_value(
        conf.get('block_trash_deletes', 'off'))

    def filt(app):
        return UndeleteMiddleware(app, trash_prefix=trash_prefix,
                                  trash_lifetime=trash_lifetime,
                                  block_trash_deletes=block_trash_deletes)
    return filt
