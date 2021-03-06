#!/usr/bin/env python

#  Copyright (c) 2014-present, Facebook, Inc.
#  All rights reserved.
#
#  This source code is licensed under the BSD-style license found in the
#  LICENSE file in the root directory of this source tree. An additional grant
#  of patent rights can be found in the PATENTS file in the same directory.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import ast
import json
import logging
import os
import sys
import uuid
import subprocess

from gentable import *
from utils import platform

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.append(SCRIPT_DIR + "/../tests")

# the log format for the logging module
LOG_FORMAT = "%(levelname)s [Line %(lineno)d]: %(message)s"

CANONICAL_PLATFORMS = {
    "specs": "All Platforms",
    "darwin": "Darwin (Apple OS X)",
    "linux": "Ubuntu, CentOS",
    "utility": "Utility",
    "windows": "Microsoft Windows",
    "posix": "POSIX-compatible Plaforms",
    "yara": "YARA",
    "lldpd": "LLDPD",
    "sleuthkit": "The Sleuth Kit"
}

TEMPLATE_API_DEFINITION = """
{
"tables": %s,
"events": [
]
}
"""


class NoIndent(object):

    """Special instance checked object for removing json newlines."""

    def __init__(self, value):
        self.value = value
        if 'type' in self.value and isinstance(self.value['type'], DataType):
            self.value['type'] = str(self.value['type'])


class Encoder(json.JSONEncoder):

    """
    Newlines are such a pain in json-generated output.
    Use this custom encoder to produce pretty json multiplexed with a more
    raw json output within.
    """

    def __init__(self, *args, **kwargs):
        super(Encoder, self).__init__(*args, **kwargs)
        self.kwargs = dict(kwargs)
        del self.kwargs['indent']
        self._replacement_map = {}

    def default(self, o):
        if isinstance(o, NoIndent):
            key = uuid.uuid4().hex
            self._replacement_map[key] = json.dumps(o.value, **self.kwargs)
            return "@@%s@@" % (key,)
        else:
            return super(Encoder, self).default(o)

    def encode(self, o):
        result = super(Encoder, self).encode(o)
        for k, v in self._replacement_map.iteritems():
            result = result.replace('"@@%s@@"' % (k,), v)
        return result


def gen_api_json(api):
    """Apply the api literal object to the template."""
    api = json.dumps(
        api, cls=Encoder, sort_keys=True, indent=1, separators=(',', ': ')
    )
    return TEMPLATE_API_DEFINITION % (api)


def gen_spec(tree):
    """Given a table tree, produce a literal of the table representation."""
    exec(compile(tree, "<string>", "exec"))
    columns = [NoIndent({
        "name": column.name,
        "type": column.type,
        "description": column.description,
        "options": column.options,
    }) for column in table.columns()]
    foreign_keys = [NoIndent({"column": key.column, "table": key.table})
                    for key in table.foreign_keys()]
    return {
        "name": table.table_name,
        "columns": columns,
        "foreign_keys": foreign_keys,
        "function": table.function,
        "description": table.description,
        "attributes": table.attributes,
        "examples": table.examples,
    }


def gen_diff(api_old_path, api_new_path):
    """Quick and dirty way to view table API changes."""
    with open(api_old_path, 'r') as fh:
        api_old = json.loads(fh.read())
    with open(api_new_path, 'r') as fh:
        api_new = json.loads(fh.read())

    # Prune table lists into maps
    old_tables = {}
    new_tables = {}
    for category in api_new["tables"]:
        for table in category["tables"]:
            new_tables["%s:%s" % (category["name"], table["name"])] = table
    for category in api_old["tables"]:
        for table in category["tables"]:
            old_tables["%s:%s" % (category["name"], table["name"])] = table

    # Iterate backwards then forward to detect added/removed.
    tables_added = []
    tables_removed = []
    columns_added = []
    columns_removed = []
    for name, table in new_tables.iteritems():
        if name not in old_tables:
            tables_added.append(name)
            continue
        for column in table["columns"]:
            old_columns = [c["name"] for c in old_tables[name]["columns"]]
            if column["name"] not in old_columns:
                columns_added.append("%s:%s:%s:%s" % (category["name"],
                                                      table["name"], column["name"], column["type"]))

    for name, table in old_tables.iteritems():
        if name not in new_tables:
            tables_removed.append(name)
            continue
        for column in table["columns"]:
            new_columns = [c["name"] for c in new_tables[name]["columns"]]
            if column["name"] not in new_columns:
                columns_removed.append("%s:%s:%s:%s" % (category["name"],
                                                        table["name"], column["name"], column["type"]))

    # Sort then pretty print (md) the changes.
    tables_added.sort()
    for name in tables_added:
        print("Added table `%s` to %s" % tuple(name.split(":")[::-1]))
    columns_added.sort()
    for name in columns_added:
        column = name.split(":")
        print("Added column `%s` (`%s`) to table `%s`" % (column[2], column[3],
                                                          column[1]))
    tables_removed.sort()
    for name in tables_removed:
        print("Removed table `%s` from %s" % tuple(name.split(":")[::-1]))
    columns_removed.sort()
    for name in columns_removed:
        column = name.split(":")
        print("Removed column `%s` (`%s`) from table `%s`" % (column[2],
                                                              column[3], column[1]))


def gen_api(tables_path, profile={}):
    blacklist = None
    blacklist_path = os.path.join(tables_path, "blacklist")
    if os.path.exists(blacklist_path):
        with open(blacklist_path, "r") as fh:
            blacklist = fh.read()

    categories = {}
    for base, _, files in os.walk(tables_path):
        for spec_file in files:
            if spec_file[0] == '.' or spec_file.find("example") == 0:
                continue
            # Exclude blacklist specific file
            if spec_file == 'blacklist':
                continue
            platform = os.path.basename(base)
            platform_name = CANONICAL_PLATFORMS[platform]
            name = spec_file.split(".table", 1)[0]
            if platform not in categories.keys():
                categories[platform] = {"name": platform_name, "tables": []}
            with open(os.path.join(base, spec_file), "rU") as fh:
                tree = ast.parse(fh.read())
                table_spec = gen_spec(tree)
                table_profile = profile.get("%s.%s" % (platform, name), {})
                table_spec["profile"] = NoIndent(table_profile)
                table_spec["blacklisted"] = is_blacklisted(table_spec["name"],
                                                           blacklist=blacklist)
                categories[platform]["tables"].append(table_spec)
    categories = [{"key": k, "name": v["name"], "tables": v["tables"]}
                  for k, v in categories.iteritems()]
    return categories


def main(argc, argv):
    parser = argparse.ArgumentParser("Generate API documentation.")
    parser.add_argument(
        "--debug", default=False, action="store_true",
        help="Output debug messages (when developing)"
    )
    parser.add_argument(
        "--tables", default="specs",
        help="Path to osquery table specs"
    )
    parser.add_argument(
        "--profile", default=None,
        help="Add the results of a profile summary to the API."
    )
    parser.add_argument(
        "--diff", default=False, action="store_true",
        help="Compare API changes API_PREVIOUS API_CURRENT"
    )
    parser.add_argument(
        "--output", default=False, action="store_true",
        help="Create output file as the version tagged."
    )
    parser.add_argument(
        "--directory", default=".",
        help="Directory to use for the output file."
    )
    parser.add_argument("vars", nargs="*")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(format=LOG_FORMAT, level=logging.DEBUG)
    else:
        logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)

    if args.diff:
        if len(args.vars) < 2:
            logging.error("If using --diff you must supply API_OLD API_NEW")
            exit(1)
        gen_diff(args.vars[0], args.vars[1])
        exit(0)

    if not os.path.exists(args.tables):
        logging.error("Cannot find path: %s" % (args.tables))
        exit(1)

    profile = {}
    if args.profile is not None:
        if not os.path.exists(args.profile):
            logging.error("Cannot find path: %s" % (args.profile))
            exit(1)
        with open(args.profile, "r") as fh:
            try:
                profile = json.loads(fh.read())
            except Exception as e:
                logging.error("Cannot parse profile data: %s" % (str(e)))
                exit(2)

    # Read in the optional list of blacklisted tables, then generate
    # categories.
    api = gen_api(args.tables, profile)

    # Output file will be the version with json extension, otherwise stdout
    if args.output:
        print('[+] creating tables json')
        cmd = ['git', 'describe', '--tags', 'HEAD']
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        out, err = proc.communicate()
        output_file = out.split("\n")[0] + ".json"
        if args.directory[-1:] == '/':
            output_path = args.directory + output_file
        else:
            output_path = args.directory + '/' + output_file

        with open(output_path, 'w') as f:
            print(gen_api_json(api), file=f)
        print('[+] tables json file created at %s' % (output_path))
    else:
        print(gen_api_json(api))


if __name__ == "__main__":
    SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
    main(len(sys.argv), sys.argv)
