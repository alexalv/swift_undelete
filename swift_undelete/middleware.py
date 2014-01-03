# Copyright (c) 2013 Samuel N. Merritt <sam@swiftstack.com>
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

 * allow undelete to be enabled only for particular accounts or containers

"""


class UndeleteMiddleware(object):
    def __init__(self, app, account_prefix):
        self.app = app
        self.account_prefix = account_prefix

    def __call__(self, env, start_response):
        return self.app(env, start_response)


def filter_factory(global_conf, **local_conf):
    """
    Returns the WSGI filter for use with paste.deploy.

    Parameters in config:

    # value to prepend to the account in order to compute the trash location
    account_prefix = ".trash"

    """
    conf = global_conf.copy()
    conf.update(local_conf)

    account_prefix = conf.get("account_prefix", ".trash")

    def filt(app):
        return UndeleteMiddleware(app, account_prefix)
    return filt
