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
import StringIO
import gzip
from io import BytesIO
import zipfile

import boto
from boto.s3.connection import Location

from pyLibrary import convert
from pyLibrary.debugs.logs import Log
from pyLibrary.dot import wrap
from pyLibrary.env.big_data import safe_size, MAX_STRING_SIZE, GzipLines, LazyLines
from pyLibrary.meta import use_settings
from pyLibrary.times.dates import Date
from pyLibrary.times.timer import Timer


READ_ERROR = "S3 read error"
MAX_FILE_SIZE = 100*1024*1024

class File(object):
    def __init__(self, bucket, key):
        self.bucket = bucket
        self.key = key

    def read(self):
        return self.bucket.read(self.key)

    def read_lines(self):
        return self.bucket.read_lines(self.key)

    def write(self, value):
        self.bucket.write(self.key, value)

    @property
    def meta(self):
        return self.bucket.meta(self.key)


class Connection(object):
    @use_settings
    def __init__(
        self,
        aws_access_key_id,  # CREDENTIAL
        aws_secret_access_key,  # CREDENTIAL
        region=None,  # NAME OF AWS REGION, REQUIRED FOR SOME BUCKETS
        settings=None
    ):
        self.settings = settings

        try:
            if not settings.region:
                self.connection = boto.connect_s3(
                    aws_access_key_id=self.settings.aws_access_key_id,
                    aws_secret_access_key=self.settings.aws_secret_access_key
                )
            else:
                self.connection = boto.s3.connect_to_region(
                    self.settings.region,
                    aws_access_key_id=self.settings.aws_access_key_id,
                    aws_secret_access_key=self.settings.aws_secret_access_key
                )
        except Exception, e:
            Log.error("Problem connecting to S3", e)


    def __enter__(self):
        return self


    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.connection:
            self.connection.close()


    def get_bucket(self, name):
        output = SkeletonBucket()
        output.bucket = self.connection.get_bucket(name, validate=False)
        return output



class Bucket(object):
    """
    STORE JSON, OR CR-DELIMITED LIST OF JSON, IN S3
    THIS CLASS MANAGES THE ".json" EXTENSION, AND ".gz"
    (ZIP/UNZIP) SHOULD THE FILE BE BIG ENOUGH TO
    JUSTIFY IT
    """

    @use_settings
    def __init__(
        self,
        bucket,  # NAME OF THE BUCKET
        aws_access_key_id,  # CREDENTIAL
        aws_secret_access_key,  # CREDENTIAL
        region=None,  # NAME OF AWS REGION, REQUIRED FOR SOME BUCKETS
        public=False,
        debug=False,
        settings=None
    ):
        self.settings = settings
        self.connection = None
        self.bucket = None

        try:
            self.connection = Connection(settings).connection
            self.bucket = self.connection.get_bucket(self.settings.bucket, validate=False)
        except Exception, e:
            Log.error("Problem connecting to {{bucket}}", {"bucket": self.settings.bucket}, e)


    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.connection:
            self.connection.close()

    def get_key(self, key):
        return File(self, key)

    def get_meta(self, key):
        if key.endswith(".json") or key.endswith(".zip") or key.endswith(".gz"):
            Log.error("Expecting a pure key")

        try:
            metas = wrap(list(self.bucket.list(prefix=key + ".json")))
            if len(metas) == 0:
                return None
            elif len(metas) > 1:
                Log.error("multiple keys with prefix={{prefix}}\n{{list|indent}}", {"prefix": metas[0].key + ".json", "list": metas.select("key")})

            return metas[0]
        except Exception, e:
            Log.error(READ_ERROR, e)

    def keys(self, prefix=None):
        return set(strip_extension(k.key) for k in self.bucket.list(prefix=prefix))

    def metas(self, prefix=None):
        """
        RETURN THE METADATA DESCRIPTORS FOR EACH KEY
        """

        keys = self.bucket.list(prefix=prefix)
        output = [
            {
                "key": strip_extension(k.key),
                "etag": convert.quote2string(k.etag),
                "expiry_date": Date(k.expiry_date),
                "last_modified": Date(k.last_modified)
            }
            for k in keys
        ]
        return wrap(output)


    def read(self, key):
        source = self.get_meta(key)

        try:
            json = safe_size(source)
        except Exception, e:
            Log.error(READ_ERROR, e)

        if json == None:
            return None

        if source.key.endswith(".zip"):
            json = _unzip(json)
        elif source.key.endswith(".gz"):
            json = convert.zip2bytes(json)

        return convert.utf82unicode(json)

    def read_lines(self, key):
        source = self.get_meta(key)
        if source.size < MAX_STRING_SIZE:
            if source.key.endswith(".gz"):
                return GzipLines(source.read(key))
            else:
                return convert.utf82unicode(source.read(key)).split("\n")

        if source.key.endswith(".gz"):
            return LazyLines(convert.zip2bytes(source))
        else:
            return LazyLines(source)

    def write(self, key, value):
        if key.endswith(".json") or key.endswith(".zip"):
            Log.error("Expecting a pure key")

        try:
            if hasattr(value, "read"):
                storage = self.bucket.new_key(key + ".json.gz")
                string_length = len(value)
                value = convert.bytes2zip(value)
                file_length = len(value)
                Log.note("Sending contents with length {{file_length|comma}} (from string with length {{string_length|comma}})", {"file_length": file_length, "string_length":string_length})
                value.seek(0)
                storage.set_contents_from_file(value)

                if self.settings.public:
                    storage.set_acl('public-read')
                return

            if len(value) > 20 * 1000:
                self.bucket.delete_key(key + ".json")
                if isinstance(value, str):
                    value = convert.bytes2zip(value)
                    key += ".json.gz"
                else:
                    value = convert.bytes2zip(convert.unicode2utf8(value))
                    key += ".json.gz"

            else:
                self.bucket.delete_key(key + ".json.gz")
                if isinstance(value, str):
                    key += ".json"
                else:
                    key += ".json"

            storage = self.bucket.new_key(key)
            storage.set_contents_from_string(value)

            if self.settings.public:
                storage.set_acl('public-read')
        except Exception, e:
            Log.error("Problem writing {{bytes}} bytes to {{key}} in {{bucket}}", {
                "key": storage.key,
                "bucket": self.bucket.name,
                "bytes": len(value)
            }, e)

    def write_lines(self, key, *lines):
        storage = self.bucket.new_key(key + ".json.gz")

        buff = BytesIO()
        archive = gzip.GzipFile(fileobj=buff, mode='w')
        count=0
        for l in lines:
            if hasattr(l, "__iter__"):
                for ll in l:
                    archive.write(ll.encode("utf8"))
                    archive.write(b"\n")
                    count+=1
            else:
                archive.write(l.encode("utf8"))
                archive.write(b"\n")
                count += 1
        archive.close()
        file_length = buff.tell()
        buff.seek(0)
        with Timer("Sending {{count}} lines in {{file_length|comma}} bytes", {"file_length": file_length, "count": count}, debug=self.settings.debug):
            storage.set_contents_from_file(buff)

        if self.settings.public:
            storage.set_acl('public-read')
        return

    @property
    def name(self):
        return self.settings.bucket


class SkeletonBucket(Bucket):
    """
    LET CALLER WORRY ABOUT SETTING PROPERTIES
    """
    def __init__(self):
        object.__init__(self)
        self.connection = None
        self.bucket = None



def strip_extension(key):
    e = key.find(".json")
    if e == -1:
        return key
    return key[:e]



def _unzip(compressed):
    buff = StringIO.StringIO(compressed)
    archive = zipfile.ZipFile(buff, mode='r')
    return archive.read(archive.namelist()[0])
