#!/usr/bin/env python
#-*- coding: utf-8 -*-
#pylint: disable=
"""
File       : MongoIO.py
Author     : Valentin Kuznetsov <vkuznet AT gmail dot com>
Description: WMArchive Mongo storage client
"""

# futures
from __future__ import print_function, division

# system modules
import os
import json
import datetime
import traceback
import time
import traceback

# Mongo modules
from pymongo import MongoClient
from pymongo.errors import InvalidDocument, InvalidOperation, DuplicateKeyError
from pymongo.son_manipulator import SONManipulator
from bson.son import SON

# WMArchive modules
from WMArchive.Storage.BaseIO import Storage
from WMArchive.Utils.Regexp import PAT_UID
from WMArchive.Utils.Exceptions import WriteError, ReadError

def set_duplicates(docs):
    "Return duplicates FWJR ids within given set of docs"
    fwjrids = []
    wmaids = []
    for rec in docs:
        wid = -1
        fid = -1
        if  isinstance(rec, dict):
            fid = rec.get('meta_data', {}).get('fwjr_id', -1)
            wid = rec.get('wmaid', -1)
        fwjrids.append(fid)
        wmaids.append(wid)
    fdups = set([x for x in fwjrids if fwjrids.count(x) > 1])
    wdups = set([x for x in wmaids if wmaids.count(x) > 1])
    return list(fdups), list(wdups)

class WMASONManipulator(SONManipulator):
    """WMArchive MongoDB SON manipulator"""
    def __init__(self):
        SONManipulator.__init__(self)

    def transform_incoming(self, son, collection):
        "Manipulate an incoming SON object"
        if  self.will_copy():
            return SON(son)
        return son

    def transform_outgoing(self, son, collection):
        "Manipulate an outgoing SON object"
        if  self.will_copy():
            return SON(son)
        if  isinstance(son, dict) and '_id' in son:
            del son['_id']
        return son

class MongoStorage(Storage):
    "Storage based on MongoDB back-end"
    def __init__(self, uri, dbname='fwjr', collname='db', chunk_size=1000):
        "ctor with mongo uri: mongodb://host:port"
        Storage.__init__(self, uri)
        self.client = MongoClient(uri, w=1)
        self.mdb = self.client[dbname]
        self.mdb.add_son_manipulator(WMASONManipulator())
        self.collname = collname
        self.coll = self.mdb[collname]
        self.jobs = self.mdb['jobs'] # separate collection for job results
        self.log(self.coll)
        self.chunk_size = chunk_size

    def sconvert(self, spec, fields):
        "convert input spec/fields into ones suitable for MognoDB QL"
        return spec, fields

    def find_duplicates(self, wmaids):
        "Find duplicate FWJR ids from collection of docs"
        spec = {'wmaid':{'$in':wmaids}}
        fields = ['meta_data']
        fwjr_ids = []
        for doc in self.coll.find(spec, fields):
            fwjr_ids.append(doc['meta_data'])
        return fwjr_ids

    def write(self, data, safe=None):
        "Write API, return ids of stored documents"
        if  not isinstance(data, list):
            data = [data] # ensure that we got list of data
        coll = self.coll
        if  isinstance(data[0], dict) and data[0].get('dtype', None) == 'job':
            coll = self.jobs
        wmaids = self.getids(data)
        uniqids = set(wmaids)
        sts_dup = self.find_duplicates(wmaids)
        if  len(wmaids) != len(uniqids):
            fdup, wdup = set_duplicates(data)
            self.log("WARNING, found %s duplicates in given docs, FWJR ids %s, WMA ids %s, given %s, unique %s" \
                    % ( len(wmaids)-len(uniqids), json.dumps(fdup), json.dumps(wdup), len(wmaids), len(set(wmaids)) ) )
            for wid in wdup:
                for rec in data:
                    if rec['wmaid'] == wid:
                       self.log(json.dumps(rec))
        if  sts_dup:
            self.log("WARNING, found %s duplicates in STS:" % len(sts_dup))
            for rec in sts_dup:
                self.log('WARNING, duplicate record %s' % json.dumps(rec))
        for idx in range(0, len(data), self.chunk_size):
            docs = data[idx:idx+self.chunk_size]
            try:
                self.coll.insert(docs, continue_on_error=True)
            except InvalidDocument as exp:
                self.log('WARNING InvalidDocument: %s' % str(exp))
            except InvalidOperation as exp:
                self.log('WARNING InvalidOperation: %s' % str(exp))
            except DuplicateKeyError as exp:
                pass
            except Exception as exp:
                raise WriteError(str(exp))
        return wmaids

    def read(self, spec, fields=None):
        "Read API, it reads data from MongoDB storage for provided spec."
        try:
            gen = self.find(spec, fields)
            docs = [r for r in gen]
            return docs
        except Exception as exp:
            raise ReadError(str(exp))

    def find(self, spec, fields):
        """
        Find records in MongoDB storage for provided spec, returns generator
        over MongoDB collection
        """
        if  not spec:
            spec = {}
        if  isinstance(spec, list):
            spec = {'wmaid': {'$in': spec}}
            return self.jobs.find(spec)
        elif  PAT_UID.match(str(spec)):
            spec = {'wmaid': spec}
            return self.jobs.find(spec)
        if  fields:
            return self.coll.find(spec, fields)
        return self.coll.find(spec)

    def ndocs(self, spec):
        "Return number of documents for given spec"
        return self.coll.find(spec).count()

    def update(self, ids, spec):
        "Update documents with given set of document ids and update spec"
        if  len(ids) > self.chunk_size:
            for idx in range(0, len(ids), self.chunk_size):
                sub_ids = ids[idx:idx+self.chunk_size]
                doc_query = {'wmaid' : {'$in': sub_ids}}
                self.coll.update(doc_query, spec, multi=True)
        else:
            doc_query = {'wmaid' : {'$in': ids}}
            self.coll.update(doc_query, spec, multi=True)

    def remove(self, spec=None):
        "Remove documents from MongoDB for given spec"
        if  not spec:
            spec = {}
        return self.coll.remove(spec)

    def dropdb(self, dbname):
        "Remove given database from MongoDB"
        return self.client.drop_database(dbname)

    def stats(self):
        "Return statistics about MongoDB"
        return self.mdb.command("collstats", self.collname)

    def jobsids(self):
        "Return jobs ids"
        out = []
        for row in self.jobs.find():
            if  'wmaid' in row:
                out.append({'wmaid':row['wmaid']})
        return out

    def performance(self, metrics, axes, start_date=None, end_date=None, suggestions=[], **kwargs):
        """
        The performance service endpoint that the web UI relies upon to present
        aggregated performance data as documented in https://github.com/knly/WMArchiveAggregation.
        """
        start_time = time.time()

        performance_data = self.client[os.environ.get('WMARCHIVE_PERF_DB', 'aggregated')][os.environ.get('WMARCHIVE_PERF_COLL', 'performance')]

        def get_aggregation_result(cursor_or_dict):
            """
            Fallback for pymongo<3.0
            """
            if type(cursor_or_dict) is dict:
                return cursor_or_dict['result']
            return list(cursor_or_dict)

        # Valid keys in `stats.scope`
        scope_keys = [ 'workflow', 'task', 'host', 'site', 'jobtype', 'jobstate', 'acquisitionEra', 'exitCode', 'exitStep' ]

        # Construct scope
        scope = []
        timeframe_scope = []

        # Timeframes
        # TODO: build more robust date parsing
        if start_date is not None:
            timeframe_scope.append({
                '$match': {
                    'scope.start_date': { '$gte': datetime.datetime(int(start_date[0:4]), int(start_date[4:6]), int(start_date[6:8]), 0, 0, 0) },
                }
            })
        if end_date is not None:
            timeframe_scope.append({
                '$match': {
                    'scope.end_date': { '$lte': datetime.datetime(int(end_date[0:4]), int(end_date[4:6]), int(end_date[6:8]), 23, 59, 59) },
                }
            })
        scope += timeframe_scope

        # Scope
        filters = {}
        for scope_key in kwargs:
            if scope_key not in scope_keys or kwargs[scope_key] is None:
                continue
            filters[scope_key] = {
                '$match': {
                    'scope.' + scope_key: { '$regex': kwargs[scope_key] },
                }
            }
        scope += filters.values()

        # Collect suggestions
        collected_suggestions = { scope_key: map(lambda d: d['_id'], get_aggregation_result(performance_data.aggregate(timeframe_scope + [ f for k, f in filters.iteritems() if k != scope_key ] + [
            {
                '$group': {
                    '_id': '$scope.' + scope_key,
                },
            },
        ]))) for scope_key in suggestions }

        # Collect visualizations
        visualizations = {}

        ISO_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S.%LZ"

        for metric in metrics:
            visualizations[metric] = {}

            aggregation_key = 'performance.' + metric
            if metric == 'data.events':
                aggregation_key = 'events'
            elif metric == 'jobstate':
                aggregation_key = 'count'

            for axis in axes:

                if axis == '_summary':
                    group_id = None
                    label = axis
                if axis == 'time':
                    group_id = { 'start_date': '$scope.start_date', 'end_date': '$scope.end_date' }
                    label = { 'start_date': { '$dateToString': { 'format': ISO_DATE_FORMAT, 'date': '$_id.start_date' } }, 'end_date': { '$dateToString': { 'format': ISO_DATE_FORMAT, 'date': '$_id.end_date' } }, }
                else:
                    group_id = '$scope.' + axis
                    label = '$_id'

                if metric == 'jobstate':
                    aggregation_result = get_aggregation_result(performance_data.aggregate(scope + [
                        {
                            '$group': {
                                '_id': { 'axis': group_id, 'jobstate': '$scope.jobstate' },
                                'count': { '$sum': '$' + aggregation_key }
                            }
                        },
                        {
                            '$group': {
                                '_id': '$_id.axis',
                                'jobstates': {
                                    '$push': {
                                        'jobstate': '$_id.jobstate',
                                        'count': '$count'
                                    }
                                }
                            }
                        },
                        {
                            '$project': {
                                '_id': False,
                                'label': label,
                                'jobstates': '$jobstates',
                            }
                        }
                    ]))

                else:
                    aggregation_result = get_aggregation_result(performance_data.aggregate(scope + [
                        {
                            '$group': {
                                '_id': group_id,
                                'average': { '$avg': '$' + aggregation_key },
                                'count': { '$sum': '$' + aggregation_key + '_N' },
                            }
                        },
                        {
                            '$project': {
                                '_id': False,
                                'label': label,
                                'average': '$average',
                                'count': '$count',
                            }
                        }
                    ]))

                if axis == '_summary':
                    aggregation_result = aggregation_result[0] if aggregation_result else None

                visualizations[metric][axis] = aggregation_result

        status = (get_aggregation_result(performance_data.aggregate(scope + [
            {
                '$group': {
                    '_id': None,
                    'count': { '$sum': '$count' },
                    'start_date': { '$min': '$scope.start_date' },
                    'end_date': { '$max': '$scope.end_date' },
                },
            },
            {
                '$project': {
                    '_id': False,
                    'totalMatchedJobs': '$count',
                    'start_date': { '$dateToString': { 'format': ISO_DATE_FORMAT, 'date': '$start_date' } },
                    'end_date': { '$dateToString': { 'format': ISO_DATE_FORMAT, 'date': '$end_date' } },
                }
            }
        ])) or [ {} ])[0]
        status["time"] = time.time() - start_time
        status.update((get_aggregation_result(performance_data.aggregate([
            {
                '$group': {
                    '_id': None,
                    'min_date': { '$min': '$scope.start_date' },
                    'max_date': { '$max': '$scope.end_date' },
                },
            },
            {
                '$project': {
                    '_id': False,
                    'min_date': { '$dateToString': { 'format': ISO_DATE_FORMAT, 'date': '$min_date' } },
                    'max_date': { '$dateToString': { 'format': ISO_DATE_FORMAT, 'date': '$max_date' } },
                }
            }
        ])) or [ {} ])[0])

        # Collect supplementary data
        supplementaryData = {}
        if "exitCode" in axes + suggestions:
            try:
                with open(os.environ.get('WMARCHIVE_ERROR_CODES', ''), 'r') as exit_codes_file:
                    supplementaryData["exitCodes"] = json.load(exit_codes_file)
            except:
                traceback.print_exc()
        if len(metrics) == 0:
            try:
                with open(os.environ.get('WMARCHIVE_PERF_METRICS', ''), 'r') as metrics_file:
                    supplementaryData["metrics"] = json.load(metrics_file)
            except:
                traceback.print_exc()

        return {
            "status": status,
            "suggestions": collected_suggestions,
            "visualizations": visualizations,
            "supplementaryData": supplementaryData,
        }
