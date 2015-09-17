# encoding: utf-8
#
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#

from __future__ import unicode_literals
from __future__ import division
import hashlib

from active_data.app import OVERVIEW
from base_test_class import ActiveDataBaseTest
import base_test_class
from pyLibrary import convert
from pyLibrary.dot import wrap
from pyLibrary.env import elasticsearch
from pyLibrary.parsers import URL
from pyLibrary.thread.threads import Thread


class TestLoadAndSaveQueries(ActiveDataBaseTest):

    def test_save_then_load(self):

        test = {
            "data": [
                {"a": "b"}
            ],
            "query": {
                "meta": {"save": True},
                "from": base_test_class.settings.backend_es.index,
                "select": "a"
            },
            "expecting_list": {
                "meta": {
                    "format": "list"
                },
                "data": ["b"]
            }
        }

        settings = self._fill_es(test)

        bytes = convert.unicode2utf8(convert.value2json({
            "from": settings.index,
            "select": "a",
            "format": "list"
        }))
        expected_hash = convert.bytes2base64(hashlib.sha1(bytes).digest()[0:6]).replace("/", "_")
        wrap(test).expecting_list.meta.saved_as = expected_hash

        self._send_queries(settings, test, delete_index=False)

        #ENSURE THE QUERY HAS BEEN INDEXED
        container = elasticsearch.Index(index="saved_queries", settings=settings)
        container.flush()
        Thread.sleep(seconds=5)

        url = URL(self.service_url)

        response = self._try_till_response(url.scheme+"://"+url.host+":"+unicode(url.port)+"/find/"+expected_hash, data=b'')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.all_content, bytes)






