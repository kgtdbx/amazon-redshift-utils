import os
import re
import boto3
import datetime
import getpass
import math
import socket
import sys
import time
import traceback
from multiprocessing import Pool
import pg8000
import pgpasslib
import shortuuid
import ssl

try:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
except:
    pass

import redshift_utils_helper as aws_utils
import config_constants as constants

__version__ = ".9.4.0"

# timeout for retries - 100ms
RETRY_TIMEOUT = 100. / 1000

# buffer size to add to columns whose length is reduced
COL_LENGTH_EXPANSION_BUFFER = .2

# maximum length above which varchar columns should be reduced if analyze_col_width is true
STRING_REDUCTION_MAX_LENGTH_THRESHOLD = 255

# compiled regular expressions
IDENTITY_RE = re.compile(r'"identity"\((?P<current>.*), (?P<base>.*), \(?\'(?P<seed>\d+),(?P<step>\d+)\'.*\)')


class ColumnEncoder:
    def get_env_var(name, default_value):
        return os.environ[name] if name in os.environ else default_value

    # class level properties
    db_connections = {}
    db = get_env_var('PGDATABASE', None)
    db_user = get_env_var('PGUSER', None)
    db_pwd = None
    db_host = get_env_var('PGHOST', None)
    db_port = get_env_var('PGPORT', 5439)
    debug = False
    threads = 1
    analyze_col_width = False
    new_varchar_min = None
    do_execute = False
    query_slot_count = 1
    ignore_errors = False
    ssl = False
    suppress_cw = None
    cw = None
    statement_timeout = '1200000'

    # runtime properties
    schema_name = 'public'
    target_schema = None
    table_name = None
    new_dist_key = None
    new_sort_keys = None
    force = False
    drop_old_data = False
    comprows = None
    query_group = None

    def __init__(self, **kwargs):
        # set variables
        self.__dict__.update(kwargs)

        # override the password with the contents of .pgpass or environment variables
        pwd = None
        try:
            pwd = pgpasslib.getpass(kwargs[constants.DB_HOST], kwargs[constants.DB_PORT],
                                    kwargs[constants.DB_NAME], kwargs[constants.DB_USER])
        except pgpasslib.FileNotFound as e:
            pass

        if pwd is not None:
            db_pwd = pwd

        # create a cloudwatch client
        region_key = 'AWS_REGION'
        aws_region = os.environ[region_key] if region_key in os.environ else 'us-east-1'
        if "suppress_cw" not in kwargs or not kwargs["suppress_cw"]:
            try:
                cw = boto3.client('cloudwatch', region_name=aws_region)
            except Exception as e:
                if self.debug:
                    print(traceback.format_exc())

        if self.debug:
            self._comment("Redshift Column Encoding Utility Configuration")

            if "suppress_cw" in kwargs and kwargs["suppress_cw"]:
                self._comment("Suppressing CloudWatch metrics")
            else:
                if cw is not None:
                    self._comment("Created Cloudwatch Emitter in %s" % aws_region)

        # override stdout with the provided filename
        if constants.OUTPUT_FILE in kwargs:
            sys.stdout = open(kwargs.get(constants.OUTPUT_FILE), 'w+')

    def _execute_query(self, string):
        conn = self._get_pg_conn()
        cursor = conn.cursor()
        cursor.execute(string)

        try:
            results = cursor.fetchall()
        except pg8000.ProgrammingError as e:
            if "no result set" in str(e):
                return None
            else:
                raise e

        return results

    def _close_conn(self, conn):
        try:
            conn.close()
        except Exception as e:
            if self.debug:
                if 'connection is closed' not in str(e):
                    print(e)

    def _cleanup(self, conn):
        # close all connections and close the output file
        if conn is not None:
            self._close_conn(conn)

        for key in self.db_connections:
            if self.db_connections[key] is not None:
                self._close_conn(self.db_connections[key])

    def _comment(self, string):
        if string is not None:
            if re.match('.*\\n.*', string) is not None:
                print('/* [%s]\n%s\n*/\n' % (str(os.getpid()), string))
            else:
                print('-- [%s] %s' % (str(os.getpid()), string))

    def _print_statements(self, statements):
        if statements is not None:
            for s in statements:
                if s is not None:
                    print(s)

    def _get_pg_conn(self):
        pid = str(os.getpid())

        conn = None

        # get the database connection for this PID
        try:
            conn = self.db_connections[pid]
        except KeyError:
            pass

        if conn is None:
            # connect to the database
            if self.debug:
                self._comment('Connect [%s] %s:%s:%s:%s' % (pid, self.db_host, self.db_port, self.db, self.db_user))

            try:
                conn = pg8000.connect(user=self.db_user, host=self.db_host, port=self.db_port, database=self.db,
                                      password=self.db_pwd,
                                      ssl_context=ssl.create_default_context() if self.ssl is True else None,
                                      timeout=None)
                # Enable keepalives manually until pg8000 supports it
                # For future reference: https://github.com/mfenniak/pg8000/issues/149
                # TCP keepalives still need to be configured appropriately on OS level as well
                conn._usock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                conn.autocommit = True
            except Exception as e:
                print(e)
                print('Unable to connect to Cluster Endpoint')
                self._cleanup(conn)
                raise e

            aws_utils.set_search_paths(conn, self.schema_name, self.target_schema, exclude_external_schemas=True)

            if self.query_group is not None:
                set_query_group = 'set query_group to %s' % self.query_group

                if self.debug:
                    self._comment(set_query_group)

                self._run_commands(conn, [set_query_group])

            if self.query_slot_count is not None and self.query_slot_count != 1:
                set_slot_count = 'set wlm_query_slot_count = %s' % self.query_slot_count

                if self.debug:
                    self._comment(set_slot_count)

                self._run_commands(conn, [set_slot_count])

            # set a long statement timeout
            set_timeout = "set statement_timeout = '%s'" % self.statement_timeout
            if self.debug:
                self._comment(set_timeout)

            self._run_commands(conn, [set_timeout])

            # set application name
            set_name = "set application_name to 'ColumnEncodingUtility-v%s'" % __version__

            if self.debug:
                self._comment(set_name)

            self._run_commands(conn, [set_name])

            # turn off autocommit for the rest of the executions
            conn.autocommit = False

            # cache the connection
            self.db_connections[pid] = conn

        return conn

    def _get_identity(self, adsrc):
        # checks if a column defined by adsrc (column from pg_attrdef) is
        # an identity, since both identities and defaults end up in this table
        # if is identity returns (seed, step); if not returns None
        # TODO there ought be a better way than using a regex
        m = IDENTITY_RE.match(adsrc)
        if m:
            return m.group('seed'), m.group('step')
        else:
            return None

    def _get_grants(self, schema_name, table_name, current_user):
        sql = '''
            WITH priviledge AS
            (
                SELECT 'SELECT'::varchar(10) as "grant"
                UNION ALL
                SELECT 'DELETE'::varchar(10)
                UNION ALL
                SELECT 'INSERT'::varchar(10)
                UNION ALL
                SELECT 'UPDATE'::varchar(10)
                UNION ALL
                SELECT 'REFERENCES'::varchar(10)
            ),
            usr AS
            (
                SELECT usesysid, 0 as grosysid, usename, false as is_group
                FROM pg_user
                WHERE usename != 'rdsdb'
                UNION ALL
                SELECT 0, grosysid, groname, true
                FROM pg_group
            )
            SELECT nc.nspname AS table_schema, c.relname AS table_name, priviledge."grant" AS privilege_type, usr.is_group, usr.usename AS grantee
            FROM pg_class c
            JOIN pg_namespace nc ON (c.relnamespace = nc.oid)
            CROSS JOIN usr
            CROSS JOIN priviledge
            JOIN pg_user ON pg_user.usename not in ('rdsdb')
            WHERE  (c.relkind = 'r'::"char" OR c.relkind = 'v'::"char")
            AND aclcontains(c.relacl, makeaclitem(usr.usesysid, usr.grosysid, pg_user.usesysid, priviledge."grant", false))
            AND c.relname = '%s'
            AND nc.nspname = '%s'
            and grantee != '%s'
            ;
        ''' % (table_name, schema_name, current_user)

        if self.debug:
            self._comment(sql)

        grants = self._execute_query(sql)

        grant_statements = []

        for grant in grants:
            if grant[3] == True:
                grant_statements.append(
                    "grant %s on %s.%s to group \"%s\";" % (grant[2].lower(), schema_name, table_name, grant[4]))
            else:
                grant_statements.append(
                    "grant %s on %s.%s to \"%s\";" % (grant[2].lower(), schema_name, table_name, grant[4]))

        if len(grant_statements) > 0:
            return grant_statements
        else:
            if self.debug:
                self._comment('Found no table grants to extend to the new table')
            return None

    def _get_foreign_keys(self, schema_name, set_target_schema, table_name):
        has_fks = False

        fk_statement = '''SELECT /* fetching foreign key relations */ conname,
      pg_catalog.pg_get_constraintdef(cons.oid, true) as condef
     FROM pg_catalog.pg_constraint cons,
     pg_class pgc
     WHERE cons.conrelid = pgc.oid
     and pgc.oid = '%s."%s"'::regclass
     AND cons.contype = 'f'
     ORDER BY 1
    ''' % (schema_name, table_name)

        if self.debug:
            self._comment(fk_statement)

        foreign_keys = self._execute_query(fk_statement)
        fk_statements = []

        for fk in foreign_keys:
            has_fks = True
            references_clause = fk[1].replace('REFERENCES ', 'REFERENCES %s.' % set_target_schema)
            fk_statements.append(
                'alter table %s."%s" add constraint %s %s;' % (set_target_schema, table_name, fk[0], references_clause))

        if has_fks:
            return fk_statements
        else:
            return None

    def _get_primary_key(self, schema_name, set_target_schema, original_table, new_table):
        pk_statement = 'alter table %s."%s" add primary key (' % (set_target_schema, new_table)
        has_pks = False

        # get the primary key columns
        statement = '''SELECT /* fetch primary key information */
      att.attname
    FROM pg_index ind, pg_class cl, pg_attribute att
    WHERE
      cl.oid = '%s."%s"'::regclass
      AND ind.indrelid = cl.oid
      AND att.attrelid = cl.oid
      and att.attnum = ANY(string_to_array(textin(int2vectorout(ind.indkey)), ' '))
      and attnum > 0
      AND ind.indisprimary
    order by att.attnum;
    ''' % (schema_name, original_table)

        if self.debug:
            self._comment(statement)

        pks = self._execute_query(statement)

        for pk in pks:
            has_pks = True
            pk_statement = pk_statement + pk[0] + ','

        pk_statement = pk_statement[:-1] + ');'

        if has_pks:
            return pk_statement
        else:
            return None

    def _get_table_desc(self, schema_name, table_name):
        # get the table definition from the dictionary so that we can get relevant details for each column
        statement = '''select /* fetching column descriptions for table */ "column", type, encoding, distkey, sortkey, "notnull", ad.adsrc
     from pg_table_def de, pg_attribute at LEFT JOIN pg_attrdef ad ON (at.attrelid, at.attnum) = (ad.adrelid, ad.adnum)
     where de.schemaname = '%s'
     and de.tablename = '%s'
     and at.attrelid = '%s."%s"'::regclass
     and de.column = at.attname
    ''' % (schema_name, table_name, schema_name, table_name)

        if self.debug:
            self._comment(statement)

        description = self._execute_query(statement)

        descr = {}
        for row in description:
            if self.debug:
                self._comment("Table Description: %s" % str(row))
            descr[row[0]] = row

        return descr

    def _get_count_raw_columns(self, schema_name, table_name):
        # count the number of raw encoded columns which are not the sortkey, from the dictionary
        statement = '''select /* getting count of raw columns in table */ count(9) count_raw_columns
          from pg_table_def
          where schemaname = '%s'
            and lower(encoding) in ('raw','none')
            and sortkey != 1
            and tablename = '%s'
    ''' % (schema_name, table_name)

        if self.debug:
            self._comment(statement)

        description = self._execute_query(statement)

        return description

    def _run_commands(self, conn, commands):
        cursor = conn.cursor()

        for c in commands:
            if c is not None:
                self._comment('[%s] Running %s' % (str(os.getpid()), c))
                try:
                    if c.count(';') > 1:
                        subcommands = c.split(';')

                        for s in subcommands:
                            if s is not None and s != '':
                                cursor.execute(s.replace("\n", ""))
                    else:
                        cursor.execute(c)
                    self._comment('Success.')
                except Exception as e:
                    # cowardly bail on errors
                    conn.rollback()
                    print(traceback.format_exc())
                    return False

        return True

    def _reduce_column_length(self, col_type, column_name, table_name):
        set_col_type = col_type

        # analyze the current size length for varchar columns and return early if they are below the threshold
        if "varchar" in col_type:
            curr_col_length = int(re.search(r'\d+', col_type).group())
            if curr_col_length < STRING_REDUCTION_MAX_LENGTH_THRESHOLD:
                return col_type
            else:
                col_len_statement = 'select /* computing max column length */ max(octet_length("%s")) from %s."%s"' % (
                    column_name, self.schema_name, table_name)
        else:
            col_len_statement = 'select /* computing max column length */ max(abs("%s")) from %s."%s"' % (
                column_name, self.schema_name, table_name)

        if self.debug:
            self._comment(col_len_statement)

        self._comment("Analyzing max length of column '%s' for table '%s.%s' " % (
            column_name, self.schema_name, table_name))

        # run the analyze in a loop, because it could be locked by another process modifying rows
        # and get a timeout
        col_len_result = None
        col_len_retry = 10
        col_len_attempt_count = 0
        col_len_last_exception = None

        while col_len_attempt_count < col_len_retry and col_len_result is None:
            try:
                col_len_result = self._execute_query(col_len_statement)
                col_max_len = col_len_result[0][0]
                if col_max_len is None:
                    col_max_len = 0
            except KeyboardInterrupt:
                # To handle Ctrl-C from user
                self._cleanup(self._get_pg_conn())
                return constants.TERMINATED_BY_USER
            except Exception as e:
                print(e)
                col_len_attempt_count += 1
                col_len_last_exception = e

                # Exponential Backoff
                time.sleep(2 ** col_len_attempt_count * RETRY_TIMEOUT)

        if col_len_result is None:
            if col_len_last_exception is not None:
                print("Unable to determine length of %s for table %s due to Exception %s" % (
                    column_name, table_name, col_len_last_exception.message))
                raise col_len_last_exception
            else:
                print(
                    "Unable to determine length of %s for table %s due to Null response to query. No changes will be made" % (
                        column_name, table_name))

        if "varchar" in col_type:
            new_column_len = int(col_max_len * (1 + COL_LENGTH_EXPANSION_BUFFER))

            # if the new length would be greater than varchar(max) then return the current value - no changes
            if new_column_len > 65535:
                return col_type

            # if the new length would be smaller than the specified new varchar minimum then set to varchar minimum
            if new_column_len < self.new_varchar_min:
                new_column_len = self.new_varchar_min

            # if the new length would be 0 then return the current value - no changes
            if new_column_len == 0:
                return col_type

            if self.debug:
                self._comment(
                    "Max width of character column '%s' for table '%s.%s' is %d. Current width is %d. Setting new size to %s" % (
                        column_name, self.schema_name, table_name, col_max_len,
                        curr_col_length, new_column_len))

            if new_column_len < curr_col_length:
                set_col_type = re.sub(str(curr_col_length), str(new_column_len), col_type)
        else:
            # Test to see if largest value is smaller than largest value of smallint (2 bytes)
            if col_max_len * (1 + COL_LENGTH_EXPANSION_BUFFER) <= int(math.pow(2, 15) - 1) and col_type != "smallint":

                set_col_type = re.sub(col_type, "smallint", col_type)
            # Test to see if largest value is smaller than largest value of smallint (4 bytes)
            elif col_max_len * (1 + COL_LENGTH_EXPANSION_BUFFER) <= int(math.pow(2, 31) - 1) and col_type != "integer":
                set_col_type = re.sub(col_type, "integer", col_type)

        return set_col_type

    def analyze(self, table_info):
        schema_name = table_info[0]
        table_name = table_info[1]
        dist_style = table_info[4]
        owner = table_info[5]
        if len(table_info) > 6:
            table_comment = table_info[6]

        # get the count of columns that have raw encoding applied
        table_unoptimised = False
        count_unoptimised = 0
        encodings_modified = False
        output = self._get_count_raw_columns(schema_name, table_name)

        if output is None:
            print("Unable to determine potential RAW column encoding for %s" % table_name)
            return constants.ERROR
        else:
            for row in output:
                if row[0] > 0:
                    table_unoptimised = True
                    count_unoptimised += row[0]

        if not table_unoptimised and not self.force:
            self._comment("Table %s.%s does not require encoding optimisation" % (schema_name, table_name))
            return constants.OK
        else:
            self._comment("Table %s.%s contains %s unoptimised columns" % (schema_name, table_name, count_unoptimised))
            if self.force:
                self._comment("Using Force Override Option")

            statement = 'analyze compression %s."%s"' % (schema_name, table_name)

            if self.comprows is not None:
                statement = statement + (" comprows %s" % int(self.comprows))

            try:
                if self.debug:
                    self._comment(statement)

                self._comment("Analyzing Table '%s.%s'" % (schema_name, table_name,))

                # run the analyze in a loop, because it could be locked by another process modifying rows and get a timeout
                analyze_compression_result = None
                analyze_retry = 10
                attempt_count = 0
                last_exception = None
                while attempt_count < analyze_retry and analyze_compression_result is None:
                    try:
                        analyze_compression_result = self._execute_query(statement)
                        # Commiting otherwise anaylze keep an exclusive lock until a commit arrive which can be very long
                        self._execute_query('commit;')
                    except KeyboardInterrupt:
                        # To handle Ctrl-C from user
                        self._cleanup(self._get_pg_conn())
                        return constants.TERMINATED_BY_USER
                    except Exception as e:
                        self._execute_query('rollback;')
                        print(e)
                        attempt_count += 1
                        last_exception = e

                        # Exponential Backoff
                        time.sleep(2 ** attempt_count * RETRY_TIMEOUT)

                if analyze_compression_result is None:
                    if last_exception is not None:
                        print("Unable to analyze %s due to Exception %s" % (table_name, last_exception.message))
                    else:
                        print("Unknown Error")
                    return constants.ERROR

                if self.target_schema is None:
                    set_target_schema = schema_name
                else:
                    set_target_schema = self.target_schema

                if set_target_schema == schema_name:
                    target_table = '%s_$mig' % table_name
                else:
                    target_table = table_name

                create_table = 'begin;\nlock table %s."%s";\ncreate table %s."%s"(' % (
                    schema_name, table_name, set_target_schema, target_table,)

                # query the table column definition
                descr = self._get_table_desc(schema_name, table_name)

                encode_columns = []
                statements = []
                sortkeys = {}
                has_zindex_sortkeys = False
                has_identity = False
                non_identity_columns = []
                fks = []
                table_distkey = None
                table_sortkeys = []
                new_sortkey_arr = [t.strip() for t in
                                   self.new_sort_keys.split(',')] if self.new_sort_keys is not None else []

                # count of suggested optimizations
                count_optimized = 0
                # process each item given back by the analyze request
                for row in analyze_compression_result:
                    if self.debug:
                        self._comment("Analyzed Compression Row State: %s" % str(row))
                    col = row[1]
                    row_sortkey = descr[col][4]

                    # compare the previous encoding to the new encoding
                    # don't use new encoding for first sortkey
                    datatype = descr[col][1]
                    new_encoding = row[2]
                    new_encoding = new_encoding if not abs(row_sortkey) == 1 else 'raw'
                    old_encoding = descr[col][2]
                    old_encoding = 'raw' if old_encoding == 'none' else old_encoding
                    if new_encoding != old_encoding:
                        encodings_modified = True
                        count_optimized += 1

                    # fix datatypes from the description type to the create type
                    col_type = descr[col][1].replace('character varying', 'varchar').replace('without time zone', '')

                    # check whether columns are too wide
                    if self.analyze_col_width and ("varchar" in col_type or "int" in col_type):
                        new_col_type = self._reduce_column_length(col_type, descr[col][0], table_name)
                        if new_col_type != col_type:
                            col_type = new_col_type
                            encodings_modified = True

                    # link in the existing distribution key, or set the new one
                    row_distkey = descr[col][3]
                    if table_name is not None and self.new_dist_key is not None:
                        if col == self.new_dist_key:
                            distkey = 'DISTKEY'
                            dist_style = 'KEY'
                            table_distkey = col
                        else:
                            distkey = ''
                    else:
                        if str(row_distkey).upper()[0] == 'T':
                            distkey = 'DISTKEY'
                            dist_style = 'KEY'
                            table_distkey = col
                        else:
                            distkey = ''

                    # link in the existing sort keys, or set the new ones
                    if table_name is not None and len(new_sortkey_arr) > 0:
                        if col in new_sortkey_arr:
                            sortkeys[new_sortkey_arr.index(col) + 1] = col
                            table_sortkeys.append(col)
                    else:
                        if row_sortkey != 0:
                            # add the absolute ordering of the sortkey to the list of all sortkeys
                            sortkeys[abs(row_sortkey)] = col
                            table_sortkeys.append(col)

                            if row_sortkey < 0:
                                has_zindex_sortkeys = True

                    # don't compress first sort key column. This will be set on the basis of the existing sort key not
                    # being modified, or on the assignment of the new first sortkey
                    if (abs(row_sortkey) == 1 and len(new_sortkey_arr) == 0) or (
                            col in table_sortkeys and table_sortkeys.index(col) == 0):
                        compression = 'RAW'
                    else:
                        compression = new_encoding

                    # extract null/not null setting
                    col_null = descr[col][5]

                    if str(col_null).upper() == 'TRUE':
                        col_null = 'NOT NULL'
                    else:
                        col_null = ''

                    # get default or identity syntax for this column
                    default_or_identity = descr[col][6]
                    if default_or_identity:
                        ident_data = self._get_identity(default_or_identity)
                        if ident_data is None:
                            default_value = 'default %s' % default_or_identity
                            non_identity_columns.append('"%s"' % col)
                        else:
                            default_value = 'identity (%s, %s)' % ident_data
                            has_identity = True
                    else:
                        default_value = ''
                        non_identity_columns.append('"%s"' % col)

                    if self.debug:
                        self._comment("Column %s will be encoded as %s (previous %s)" % (
                            col, compression, old_encoding))

                    # add the formatted column specification
                    encode_columns.extend(['"%s" %s %s %s encode %s %s'
                                           % (col, col_type, default_value, col_null, compression, distkey)])

                # abort if a new distkey was set but we couldn't find it in the set of all columns
                if self.new_dist_key is not None and table_distkey is None:
                    msg = "Column '%s' not found when setting new Table Distribution Key" % self.new_dist_key
                    self._comment(msg)
                    raise Exception(msg)

                # abort if new sortkeys were set but we couldn't find them in the set of all columns
                if self.new_sort_keys is not None and len(table_sortkeys) != len(new_sortkey_arr):
                    if self.debug:
                        self._comment("Requested Sort Keys: %s" % new_sortkey_arr)
                        self._comment("Resolved Sort Keys: %s" % table_sortkeys)
                    msg = "Column resolution of sortkeys '%s' not found when setting new Table Sort Keys" % new_sortkey_arr
                    self._comment(msg)
                    raise Exception(msg)

                # if this table's encodings have not changed, then don't do a modification, unless force options is set
                if (not self.force) and (not encodings_modified):
                    self._comment("Column Encoding resulted in an identical table - no changes will be made")
                else:
                    self._comment("Column Encoding will be modified for %s.%s" % (schema_name, table_name))

                    # add all the column encoding statements on to the create table statement, suppressing the leading
                    # comma on the first one
                    for i, s in enumerate(encode_columns):
                        create_table += '\n%s%s' % ('' if i == 0 else ',', s)

                    create_table = create_table + '\n)\n'

                    # add diststyle all if needed
                    if dist_style == 'ALL':
                        create_table = create_table + 'diststyle all\n'

                    # add sort key as a table block to accommodate multiple columns
                    if len(sortkeys) > 0:
                        if self.debug:
                            self._comment("Adding Sortkeys: %s" % sortkeys)
                        sortkey = '%sSORTKEY(' % ('INTERLEAVED ' if has_zindex_sortkeys else '')

                        for i in range(1, len(sortkeys) + 1):
                            sortkey = sortkey + sortkeys[i]

                            if i != len(sortkeys):
                                sortkey = sortkey + ','
                            else:
                                sortkey = sortkey + ')\n'
                        create_table = create_table + (' %s ' % sortkey)

                    create_table = create_table + ';'

                    # run the create table statement
                    statements.extend([create_table])

                    # get the primary key statement
                    statements.extend([self._get_primary_key(schema_name, set_target_schema, table_name, target_table)])

                    # set the table owner
                    statements.extend(['alter table %s."%s" owner to "%s";' % (set_target_schema, target_table, owner)])

                    if table_comment is not None:
                        statements.extend(
                            ['comment on table %s."%s" is \'%s\';' % (set_target_schema, target_table, table_comment)])

                    # insert the old data into the new table
                    # if we have identity column(s), we can't insert data from them, so do selective insert
                    if has_identity:
                        source_columns = ', '.join(non_identity_columns)
                        mig_columns = '(' + source_columns + ')'
                    else:
                        source_columns = '*'
                        mig_columns = ''

                    insert = 'insert into %s."%s" %s select %s from %s."%s"' % (set_target_schema,
                                                                                target_table,
                                                                                mig_columns,
                                                                                source_columns,
                                                                                schema_name,
                                                                                table_name)
                    if len(table_sortkeys) > 0:
                        insert = "%s order by \"%s\";" % (insert, ",".join(table_sortkeys).replace(',', '\",\"'))
                    else:
                        insert = "%s;" % (insert)

                    statements.extend([insert])

                    # analyze the new table
                    analyze = 'analyze %s."%s";' % (set_target_schema, target_table)
                    statements.extend([analyze])

                    if set_target_schema == schema_name:
                        # rename the old table to _$old or drop
                        if self.drop_old_data:
                            drop = 'drop table %s."%s" cascade;' % (set_target_schema, table_name)
                        else:
                            # the alter table statement for the current data will use the first 104 characters of the
                            # original table name, the current datetime as YYYYMMDD and a 10 digit random string
                            drop = 'alter table %s."%s" rename to "%s_%s_%s_$old";' % (
                                set_target_schema, table_name, table_name[0:104],
                                datetime.date.today().strftime("%Y%m%d"),
                                shortuuid.ShortUUID().random(length=10))

                        statements.extend([drop])

                        # rename the migrate table to the old table name
                        rename = 'alter table %s."%s" rename to "%s";' % (set_target_schema, target_table, table_name)
                        statements.extend([rename])

                    # add foreign keys
                    fks = self._get_foreign_keys(schema_name, set_target_schema, table_name)

                    # add grants back
                    grants = self._get_grants(schema_name, table_name, self.db_user)
                    if grants is not None:
                        statements.extend(grants)

                    statements.extend(['commit;'])

                    if self.do_execute:
                        if not self._run_commands(self._get_pg_conn(), statements):
                            if not self.ignore_errors:
                                if self.debug:
                                    print("Error running statements: %s" % (str(statements),))
                                return constants.ERROR

                        # emit a cloudwatch metric for the table
                        if self.cw is not None:
                            dimensions = [
                                {'Name': 'ClusterIdentifier', 'Value': self.db_host.split('.')[0]},
                                {'Name': 'TableName', 'Value': table_name}
                            ]
                            aws_utils.put_metric(self.cw, 'Redshift', 'ColumnEncodingModification', dimensions, None, 1,
                                                 'Count')
                            if self.debug:
                                self._comment("Emitted Cloudwatch Metric for Column Encoded table")
                    else:
                        self._comment("No encoding modifications run for %s.%s" % (schema_name, table_name))
            except Exception as e:
                print('Exception %s during analysis of %s' % (e, table_name))
                print(traceback.format_exc())
                return constants.ERROR

            self._print_statements(statements)

            return constants.OK, fks, encodings_modified

    def run(self, schema_name: str = 'public', target_schema: str = None, table_name: str = None,
            new_dist_key: str = None,
            new_sort_keys: list = None,
            force: bool = None,
            drop_old_data: bool = None,
            comprows: int = None,
            query_group: str = None):
        # set class properties for arguments that are overridden here
        args = locals().copy()
        for argName in args:
            if argName != 'self':
                if args.get(argName) is not None:
                    setattr(self, argName, args.get(argName))

        # get a connection for the controlling processes
        master_conn = self._get_pg_conn()

        if master_conn is None or master_conn == constants.ERROR:
            return constants.NO_CONNECTION

        self._comment("Connected to %s:%s:%s as %s" % (self.db_host, self.db_port, self.db, self.db_user))
        if self.table_name is not None:
            snippet = "Table '%s'" % self.table_name
        else:
            snippet = "Schema '%s'" % self.schema_name

        self._comment("Analyzing %s for Columnar Encoding Optimisations with %s Threads..." % (snippet, self.threads))

        if self.do_execute:
            if self.drop_old_data and not self.force:
                really_go = getpass.getpass(
                    "This will make irreversible changes to your database, and cannot be undone. Type 'Yes' to continue: ")

                if not really_go == 'Yes':
                    print("Terminating on User Request")
                    return constants.TERMINATED_BY_USER

            self._comment("Recommended encoding changes will be applied automatically...")
        else:
            pass

        # process the table name to support multiple items
        if self.table_name is not None:
            tables = ""
            if self.table_name is not None and ',' in self.table_name:
                for t in self.table_name.split(','):
                    tables = tables + "'" + t + "',"

                tables = tables[:-1]
            else:
                tables = "'" + self.table_name + "'"

        if self.table_name is not None:
            statement = '''select pgn.nspname::text as schema, trim(a.name) as table, b.mbytes, a.rows, decode(pgc.reldiststyle,0,'EVEN',1,'KEY',8,'ALL') dist_style, TRIM(pgu.usename) "owner", pgd.description
    from (select db_id, id, name, sum(rows) as rows from stv_tbl_perm a group by db_id, id, name) as a
    join pg_class as pgc on pgc.oid = a.id
    left outer join pg_description pgd ON pgd.objoid = pgc.oid and pgd.objsubid = 0
    join pg_namespace as pgn on pgn.oid = pgc.relnamespace
    join pg_user pgu on pgu.usesysid = pgc.relowner
    join (select tbl, count(*) as mbytes
    from stv_blocklist group by tbl) b on a.id=b.tbl
    and pgn.nspname::text ~ '%s' and pgc.relname in (%s)
            ''' % (self.schema_name, tables)
        else:
            # query for all tables in the schema ordered by size descending
            self._comment("Extracting Candidate Table List...")

            statement = '''select pgn.nspname::text as schema, trim(a.name) as table, b.mbytes, a.rows, decode(pgc.reldiststyle,0,'EVEN',1,'KEY',8,'ALL') dist_style, TRIM(pgu.usename) "owner", pgd.description
    from (select db_id, id, name, sum(rows) as rows from stv_tbl_perm a group by db_id, id, name) as a
    join pg_class as pgc on pgc.oid = a.id
    left outer join pg_description pgd ON pgd.objoid = pgc.oid and pgd.objsubid = 0
    join pg_namespace as pgn on pgn.oid = pgc.relnamespace
    join pg_user pgu on pgu.usesysid = pgc.relowner
    join (select tbl, count(*) as mbytes
    from stv_blocklist group by tbl) b on a.id=b.tbl
    where pgn.nspname::text  ~ '%s'
      and a.name::text SIMILAR TO '[A-Za-z0-9_]*'
    order by 2;
            ''' % (self.schema_name,)

        if self.debug:
            self._comment(statement)

        query_result = self._execute_query(statement)

        if query_result is None:
            self._comment("Unable to issue table query - aborting")
            return constants.ERROR

        table_names = []
        for row in query_result:
            table_names.append(row)

        self._comment("Analyzing %s table(s) which contain allocated data blocks" % (len(table_names)))

        if self.debug:
            [self._comment(str(x)) for x in table_names]

        result = []

        if table_names is not None:
            # we'll use a Pool to process all the tables with multiple threads, or just sequentially if 1 thread is requested
            if self.threads > 1:
                # setup executor pool
                p = Pool(self.threads)

                try:
                    # run all concurrent steps and block on completion
                    result = p.map(self.analyze, table_names)
                except KeyboardInterrupt:
                    # To handle Ctrl-C from user
                    p.close()
                    p.terminate()
                    self._cleanup(master_conn)
                    return constants.TERMINATED_BY_USER
                except:
                    print(traceback.format_exc())
                    p.close()
                    p.terminate()
                    self._cleanup(master_conn)
                    return constants.ERROR

                p.terminate()
            else:
                for t in table_names:
                    result.append(self.analyze(t))
        else:
            self._comment("No Tables Found to Analyze")

        # return any non-zero worker output statuses
        modified_tables = 0
        for ret in result:
            if isinstance(ret, (list, tuple)):
                return_code = ret[0]
                fk_commands = ret[1]
                modified_tables = modified_tables + 1 if ret[2] else modified_tables
            else:
                return_code = ret
                fk_commands = None

            if fk_commands is not None and len(fk_commands) > 0:
                self._print_statements(fk_commands)

                if self.do_execute:
                    if not self._run_commands(master_conn, fk_commands):
                        if not self.ignore_errors:
                            print("Error running commands %s" % (fk_commands,))
                            return constants.ERROR

            if return_code != constants.OK:
                print("Error in worker thread: return code %d. Exiting." % (return_code,))
                return return_code

        self._comment("Performed modification of %s tables" % modified_tables)

        if self.do_execute:
            if not master_conn.commit():
                return constants.ERROR

        self._comment('Processing Complete')
        self._cleanup(master_conn)

        return constants.OK