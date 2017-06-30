#!/usr/bin/env python
'''
Usage:
    import_xml2_obd.py <xml file or directory> <site_url> > <jsonl file>
    import_xml2obd.py upload <site_url> <api_key> <jsonl file> <doc directory>
    import_xml2obd.py pull <conf file> <output directory>

    sample conf file:
    [storage]
    source = azure://user:key@container
    dest = azure://user:key@container
'''
import argparse
import configparser
import os
import glob
import time
from datetime import datetime
import sys
import json
from collections import defaultdict, OrderedDict
from lxml import etree
import yaml
import uuid

from azure.storage.blob import BlockBlobService
from azure.common import AzureMissingResourceHttpError
from azure.storage.blob import ContentSettings

import urllib2
import ckanapi
import ckan
from ckanapi.errors import CKANAPIError
from ckan.logic import (NotAuthorized, NotFound)
import traceback

audience = None
canada_resource_type = None
canada_subject = None
canada_resource_language = None
canada_resource_format = None


def read_presets(filename):
    with open(filename, 'r') as f:
        _presets = yaml.load(f)
    return _presets


def read_json(filename):
    content=[]
    with open(filename) as f:
        for line in f:
            rec = json.loads(line)
            content.append(rec)
    return content


def read_xml(filename):
    root = etree.parse(filename).getroot()
    i = 0
    refs, extras = {}, {}
    assert(root.tag=="enterpriseLibrary")


    variant = root.find('application').find('parent').find('item').find(
                    'variants').find('variant')
    start = variant.find('properties')

    for l in start.findall('property'):
        if l.find('value').text:
            refs[l.attrib['name']] = l.find('value').text
    for c in start.findall('propertyGroup'):
        assert(c.attrib['name'] == 'customMetadata')
        for row in c.findall('propertyRow'):
            r = {}
            for l in row.findall('property'):
                r[l.attrib['name']] = l.find('value').text
            if r['metadata']:
                extras.update(r)

    return refs, extras


def desc(k,v):
    if 'English' in k:
        sub_key='en'
    elif 'French' in k:
        sub_key='fr'
    else:
        raise Exception('no such desc')
    return 'notes_translated', {sub_key:v}


def title(k,v):
    if 'English' in k:
        sub_key='en'
    elif 'French' in k:
        sub_key='fr'
    else:
        raise Exception('no such desc')

    # guess: remove file extension and underscores
    titles = v.split('.')
    if len(titles[-1]) <=4:
        v = '.'.join(titles[:-1])
    v = v.replace('_', ' ')

    return 'title_translated', {sub_key:v}

organizations = {}


def owner_org(k,v):
    v = v.split('|')[0].strip()
    if v =='Public Works and Government Services Canada':
        v='Public Services and Procurement Canada'
    id = organizations.get(v, None)
    if not id:
        raise Exception('No such organiztion ' + v)
    return 'owner_org', id


def _get_choices_value(preset, val):
    name = preset['field_name']
    res = []
    for item in preset['choices']:
        for label in item['label'].values():
            if label in val:
                res.append(item['value'])
                break
    return name, res


def _get_single_choices_value(preset, val):
    name = preset['field_name']
    res = []
    for item in preset['choices']:
        for label in item['label'].values():
            if label in val:
                res.append(item['value'])
                break
    return name, res[0]

xml2obd=OrderedDict([
    ('Description English', desc),
    ('Description French', desc),
    ('Title English', title),
    ('Title French', title),
    ('Audience', lambda k,v: _get_choices_value(audience, v),),
    ('Subject', lambda k,v: _get_choices_value(canada_subject, v),),
    ('Publisher Organization', owner_org),

    ('Date Created', lambda k, v: ('metadata_created',v)),
    ('Date Modified', lambda k, v: ('metadata_modified',v)),
    ('Classification Code', lambda k, v: ('doc_classification_code',v)),
    ('Creator', lambda k, v: ('author',v)),
    ('retentionTrigger', None),
    ('retentionPeriod', None),
    ('Status Date', None),
])

xml2resource={
    'Resource Type': lambda k,v: _get_single_choices_value(canada_resource_type, v),
    'Record Date': lambda k,v: ('created', v),
    'Unique ID': lambda k,v: ('unique_identifier', v),
    'Language': lambda k,v: _get_choices_value(canada_resource_language, v),
}


def xml_obd_mapping(dict_data, map_dict):
    res = {}
    for k,f in map_dict.iteritems():
        if k in dict_data:
            v = dict_data[k]
            dict_data.pop(k)
            if not f:
                continue
            new_key, new_val = f(k,v)
            val = res.get(new_key, None)
            if isinstance(val,dict):
                val.update(new_val)
            elif isinstance(val, list):
                for _val in new_val:
                    val.append(_val)
            elif not new_val:
                pass
            else:
                res[new_key] = new_val
    return res


def get_preset(fields, name):
    for it in fields:
        if it.get('preset', None)==name:
            return it
    return None


def upload_resources(remote_site, api_key, jsonfile, resource_directory):
    #remote site
    site = ckanapi.RemoteCKAN(
        remote_site,
        apikey=api_key,
        user_agent='ckanapi-uploader/1.0')

    #load dataset and resource file to cloud
    ds = read_json(jsonfile)
    for rec in ds:
        try:
            target_pkg = site.action.package_show(id=rec['id'])
        except (NotFound, NotAuthorized):
            target_pkg = None
        except (CKANAPIError, urllib2.URLError), e:
            sys.stdout.write(
                json.dumps([
                    rec['id'],
                    'target error',
                    unicode(e.args)
                ]) + '\n'
            )
            raise

        try:
            #site.action.package_delete(id=rec['id'])
            #return
            #site.action.package_create(**rec)
            if target_pkg:
                site.action.package_update(**rec)
            else:
                site.action.package_create(**rec)
        except (ckan.logic.NotFound, ckanapi.errors.CKANAPIError,
                ckan.logic.ValidationError):
            traceback.print_exc()
            print(rec)
            continue
        res = rec['resources'][0]
        source = resource_directory + '/' + res['url'].split('/')[-1]
        with open(source) as f:
            try:
                rc = site.action.resource_patch(
                    id=res['id'],
                    upload=(os.path.basename(source), f))
            except ckanapi.errors.CKANAPIError:
                traceback.print_exc()
                print(rec)
                print("failed upload " + os.path.basename(source))
                continue

        print("updated for " + os.path.basename(source))


def convert(refs, filename):
    ds = xml_obd_mapping(refs, xml2obd)
    ds['collection'] = 'publication'
    release_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not ds.get('date_published'):
        ds['date_published'] = release_date
    ds['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL,
                   'http://obd.open.canada.ca/' + filename))
    ds['state'] = 'active'
    ds['type'] = 'doc'
    ds['license_id'] = "ca-ogl-lgo"
    if not ds.get('owner_org', None):
        ds['owner_org'] = "A0F0FCFC-BC3B-4696-8B6D-E7E411D55BAC"
    if not ds.get('keywords', None):
        ds['keywords']={'en':['statcan'], 'fr':['statcan']}
    if not ds.get('subject', None):
        ds['subject'] = ["information_and_communications"]
    if not ds['title_translated'].get('fr', None):
        ds['title_translated']['fr'] = ds['title_translated']['en']

    res = xml_obd_mapping(refs, xml2resource)
    res_name = filename[:-4]
    res['name_translated']={'en':res_name, 'fr':res_name}
    res['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL,
                    'http://obd.open.canada.ca/resources/' + filename))
    res['url'] = 'http://obd.open.canada.ca/' + filename[:-4]
    res['format'] = filename.split('.')[-2].upper()
    if res['format'] not in canada_resource_format:
        res['format'] = 'other'
    if not res.get('language'):
        res['language'] = ['en']
    if not res.get('resource_type'):
        res['resource_type']='guide'
    ds['resources'] = [res]
    print(json.dumps(ds))


class RemoteStorage():
    def __init__(self, user, key, container):
        self.bs = BlockBlobService(account_name=user, account_key=key)
        self.container = container

    def get_obj(self, blob_name):
        try:
            return self.bs.get_blob_properties(self.container, blob_name)
        except AzureMissingResourceHttpError:
            return None

    def download_blob(self, blob_name, localf):
        self.bs.get_blob_to_path(self.container, blob_name, localf)

    def del_blob(self, blob_name):
        try:
            self.bs.delete_blob(self.container, blob_name)
        except:
            pass

    def upload(self, blob_name, localf):
        self.bs.create_blob_from_path(
                self.container, blob_name, localf,
                content_settings=ContentSettings(content_type='text/xml'))

    def new_docs(self):
        res = []
        for blob in self.bs.list_blobs(self.container):
            res.append(blob.name)
        return res


def read_conf(filename):
    config = configparser.ConfigParser()
    config.read(filename)
    src_str = config.get('storage', 'source')
    dest_str = config.get('storage', 'dest')
    import re
    r = re.match(r'^azure://(.*):(.*)@(.*)', src_str)
    r2 = re.match(r'^azure://(.*):(.*)@(.*)', dest_str)
    return (r.group(1), r.group(2), r.group(3),
            r2.group(1), r2.group(2), r2.group(3),)


def pull_docs(conf_file, local_dir):
    ''' Iterate src container for *.xml *(doc) and download/(remove after) them to local
        directory. Upload *.xml to dest container for history tracking.
        Rename on uploading if name conflicts (name, name-timestring).
    '''
    src_user, src_key, src_container, dest_user, dest_key, dest_container = read_conf(conf_file)
    src = RemoteStorage(src_user, src_key, src_container)

    all_docs = src.new_docs()
    xmls = [x for x in all_docs if x[-4:]=='.xml' and x[:-4] in all_docs and
            x[:-4] + '.ind' in all_docs]
    docs = [x[:-4] for x in xmls]
    inds = [x + '.ind' for x in docs]
    files = xmls + docs
    for fname in files:
        print 'Downloading ',fname
        src.download_blob(fname, local_dir + '/' + fname.split('/')[-1])

    dest = RemoteStorage(dest_user, dest_key, dest_container)
    for fname in xmls:
        filename = fname.split('/')[-1]
        print 'Uploading ',filename
        dest.upload('archived-doc-xmls/' + filename,
                    local_dir + '/' + filename)

    files += inds
    for fname in files:
        pass  # src.delete_blob(fname)


def main():
    global audience, canada_resource_type,canada_subject
    global canada_resource_language, organizations
    global canada_resource_format

    # upload the generated json to remote site and upload the file
    if sys.argv[1] =='upload':
        return upload_resources(*sys.argv[2:])
    elif sys.argv[1] =='pull':
        return pull_docs(*sys.argv[2:])

    site = ckanapi.RemoteCKAN(sys.argv[2])

    doc = site.action.scheming_dataset_schema_show(type='doc')
    orgs = site.action.organization_list(all_fields=True)
    for rec in orgs:
        title = rec['title'].split('|')[0].strip()
        organizations[title] = rec['id']
    assert(len(organizations)>100)

    fields = doc['dataset_fields']
    res_fields = doc['resource_fields']

    audience = get_preset(fields, 'canada_audience')
    canada_resource_type = get_preset(res_fields, 'canada_resource_type')
    canada_subject = get_preset(fields, 'canada_subject')
    canada_resource_language = get_preset(res_fields, 'canada_resource_language')
    canada_resource_format = get_preset(res_fields, 'canada_resource_format')

    assert(audience and canada_resource_type and canada_resource_language
           and canada_subject and canada_resource_format)
    canada_resource_format = [x['value'] for x in canada_resource_format['choices']]

    filename = sys.argv[1]
    files = []
    if os.path.isfile(filename):
        files.append(filename)
    elif os.path.isdir(filename):
        files = glob.glob(filename + '/*.xml')
    assert(files)

    for filename in files:
        refs, extras = read_xml(filename)
        to_remove = {}
        to_add = {}
        for k,v in refs.iteritems():
            ks, vs = k.split('|'), v.split('|')
            if len(ks) == 2 and len(vs) ==2:
                to_remove[k] = True
                for i in range(0,2):
                    to_add[ks[i].strip()] = vs[i].strip()
        for k,v in to_remove.iteritems():
            refs.pop(k)
        refs.update(to_add)
        convert(refs, os.path.basename(filename))

if __name__=='__main__':
    main()
