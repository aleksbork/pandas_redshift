#!/usr/bin/env python3
from io import StringIO
import pandas as pd
import traceback
import psycopg2
import boto3
import sys
import os
import re
import uuid
import logging

S3_ACCEPTED_KWARGS = [
    'ACL', 'Body', 'CacheControl ', 'ContentDisposition', 'ContentEncoding', 'ContentLanguage',
    'ContentLength', 'ContentMD5', 'ContentType', 'Expires', 'GrantFullControl', 'GrantRead',
    'GrantReadACP', 'GrantWriteACP', 'Metadata', 'ServerSideEncryption', 'StorageClass',
    'WebsiteRedirectLocation', 'SSECustomerAlgorithm', 'SSECustomerKey', 'SSECustomerKeyMD5',
    'SSEKMSKeyId', 'RequestPayer', 'Tagging'
]  # Available parameters for service: https://boto3.readthedocs.io/en/latest/reference/services/s3.html#S3.Client.put_object

logging_config = {
    'logger_level': logging.INFO,
    'mask_secrets': True
}
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def set_log_level(level, mask_secrets=True):
    log_level_map = {
        'debug': logging.DEBUG,
        'info': logging.INFO,
        'warn': logging.WARN,
        'error': logging.ERROR
    }
    logging_config['logger_level'] = log_level_map[level]
    logger = logging.getLogger(__name__)
    logger.setLevel(logging_config['logger_level'])
    logging_config['mask_secrets'] = mask_secrets


def mask_aws_credentials(s):
    if logging_config['mask_secrets']:
        import re
        s = re.sub('(?<=access_key_id \')(.*)(?=\')', '*' * 8, s)
        s = re.sub('(?<=secret_access_key \')(.*)(?=\')', '*' * 8, s)
    return s


def connect_to_redshift(dbname, host, user, port=5439, **kwargs):
    global connect, cursor
    connect = psycopg2.connect(dbname=dbname,
                               host=host,
                               port=port,
                               user=user,
                               **kwargs)

    cursor = connect.cursor()


def connect_to_s3(aws_access_key_id, aws_secret_access_key, bucket, subdirectory=None, aws_iam_role=None, **kwargs):
    global s3, s3_bucket_var, s3_subdirectory_var, aws_1, aws_2, aws_token, aws_role
    s3 = boto3.resource('s3',
                        aws_access_key_id=aws_access_key_id,
                        aws_secret_access_key=aws_secret_access_key,
                        **kwargs)
    s3_bucket_var = bucket
    if subdirectory is None:
        s3_subdirectory_var = ''
    else:
        s3_subdirectory_var = subdirectory + '/'
    aws_1 = aws_access_key_id
    aws_2 = aws_secret_access_key
    aws_role = aws_iam_role
    if kwargs.get('aws_session_token'):
        aws_token = kwargs.get('aws_session_token')
    else:
        aws_token = ''


def redshift_to_pandas(sql_query, query_params=None):
    # pass a sql query and return a pandas dataframe
    cursor.execute(sql_query, query_params)
    columns_list = [desc[0] for desc in cursor.description]
    data = pd.DataFrame(cursor.fetchall(), columns=columns_list)
    return data


def get_defaults(input_type):
    """Generates defaults for different types of dataframe's columns """
    default_val = 0
    if input_type == 'object':
        default_val = 'na'
    if input_type == 'str':
        default_val = 'na'
    if input_type == 'int':
        default_val = 0
    if input_type == 'bool':
        default_val = False
    if input_type == 'float':
        default_val = 0.0
    return default_val


def invalidate_to_schema(raw_df, schema_df=None):
    """Drop or add columns to make it similar to schema_df.
    1. If  column_name doesn't exist in  schema_df- drop it;
    2. If some column name from schema_df is missing - add it, populated by  default values
    3. Set order of columns in raw_df the same as schema_df"""
    if schema_df is None or len(schema_df.index) == 0:
        # do nothing if there is no schema
        return raw_df
    cols_to_drop = []
    for raw_col_name in raw_df.columns:
        if raw_col_name not in schema_df.columns:
            cols_to_drop.append(raw_col_name)
    raw_df = raw_df.drop(cols_to_drop, axis=1)

    for schema_col_name in schema_df.columns:
        if schema_col_name not in raw_df.columns:
            raw_df[schema_col_name] = get_defaults(schema_df[schema_col_name].dtype)

    raw_df = raw_df[schema_df.columns]

    return raw_df


def validate_column_names(data_frame):
    """Validate the column names to ensure no reserved words are used.

    Arguments:
        dataframe pd.data_frame -- data to validate
    """
    rrwords = open(os.path.join(os.path.dirname(__file__),
                                'redshift_reserve_words.txt'), 'r').readlines()
    rrwords = [r.strip().lower() for r in rrwords]

    data_frame.columns = [x.lower() for x in data_frame.columns]

    for col in data_frame.columns:
        try:
            assert col not in rrwords
        except AssertionError:
            raise ValueError(
                'DataFrame column name {0} is a reserve word in redshift'
                .format(col))

    # check for spaces in the column names
    there_are_spaces = sum(
        [re.search('\s', x) is not None for x in data_frame.columns]) > 0
    # delimit them if there are
    if there_are_spaces:
        col_names_dict = {x: '"{0}"'.format(x) for x in data_frame.columns}
        data_frame.rename(columns=col_names_dict, inplace=True)
    return data_frame


def df_to_s3(data_frame, csv_name, index, save_local, delimiter, verbose=True, **kwargs):
    """Write a dataframe to S3

    Arguments:
        dataframe pd.data_frame -- data to upload
        csv_name str -- name of the file to upload
        save_local bool -- save a local copy
        delimiter str -- delimiter for csv file
    """
    extra_kwargs = {k: v for k, v in kwargs.items(
    ) if k in S3_ACCEPTED_KWARGS and v is not None}
    # create local backup
    if save_local:
        data_frame.to_csv(csv_name, index=index, sep=delimiter)
        if verbose:
            logger.info('saved file {0} in {1}'.format(csv_name, os.getcwd()))
    #
    csv_buffer = StringIO()
    data_frame.to_csv(csv_buffer, index=index, sep=delimiter)
    s3.Bucket(s3_bucket_var).put_object(
        Key=s3_subdirectory_var + csv_name, Body=csv_buffer.getvalue(),
        **extra_kwargs)
    if verbose:
        logger.info('saved file {0} in bucket {1}'.format(
            csv_name, s3_subdirectory_var + csv_name))


def pd_dtype_to_redshift_dtype(dtype):
    if dtype.startswith('int64'):
        return 'BIGINT'
    elif dtype.startswith('int'):
        return 'INTEGER'
    elif dtype.startswith('float'):
        return 'REAL'
    elif dtype.startswith('datetime'):
        return 'TIMESTAMP'
    elif dtype == 'bool':
        return 'BOOLEAN'
    else:
        return 'VARCHAR(MAX)'


def get_column_data_types(data_frame, index=False, json_columns=None):
    # [f(x) if x is not None else '' for x in xs]
    column_data_types = [pd_dtype_to_redshift_dtype(dtype.name) if col_name not in json_columns else 'SUPER'
                         for dtype, col_name in zip(data_frame.dtypes.values, data_frame.columns)]
    if index:
        column_data_types.insert(
            0, pd_dtype_to_redshift_dtype(data_frame.index.dtype.name))
    return column_data_types


def create_redshift_table(data_frame,
                          redshift_table_name,
                          column_data_types=None,
                          index=False,
                          append=False,
                          diststyle='even',
                          distkey='',
                          sort_interleaved=False,
                          sortkey='',
                          json_columns=None,
                          verbose=True):
    """Create an empty RedShift Table

    """
    if index:
        columns = list(data_frame.columns)
        if data_frame.index.name:
            columns.insert(0, data_frame.index.name)
        else:
            columns.insert(0, "index")
    else:
        columns = list(data_frame.columns)
    if column_data_types is None:
        column_data_types = get_column_data_types(data_frame, index, json_columns)
    columns_and_data_type = ', '.join(
        ['{0} {1}'.format(x, y) for x, y in zip(columns, column_data_types)])

    create_table_query = 'create table {0} ({1})'.format(
        redshift_table_name, columns_and_data_type)
    if not distkey:
        # Without a distkey, we can set a diststyle
        if diststyle not in ['even', 'all']:
            raise ValueError("diststyle must be either 'even' or 'all'")
        else:
            create_table_query += ' diststyle {0}'.format(diststyle)
    else:
        # otherwise, override diststyle with distkey
        create_table_query += ' distkey({0})'.format(distkey)
    if len(sortkey) > 0:
        if sort_interleaved:
            create_table_query += ' interleaved'
        create_table_query += ' sortkey({0})'.format(sortkey)
    if verbose:
        logger.info(create_table_query)
        logger.info('CREATING A TABLE IN REDSHIFT')
    cursor.execute('drop table if exists {0};'.format(redshift_table_name))
    cursor.execute(create_table_query)
    connect.commit()


def s3_to_redshift(redshift_table_name, csv_name, rs_iam_role, delimiter=',', quotechar='"',
                   dateformat='auto', timeformat='auto', region='', parameters='', verbose=True):
    bucket_name = 's3://{0}/{1}'.format(
        s3_bucket_var, s3_subdirectory_var + csv_name)

    if rs_iam_role:  # IAM role for the redhsift cluter to access S3 bucket
        authorization = """
        iam_role '{0}'
        """.format(rs_iam_role)
    else:
        if aws_1 and aws_2:
            authorization = """
        access_key_id '{0}'
        secret_access_key '{1}'
        """.format(aws_1, aws_2)
        elif aws_role:  # IAM role for the user account to access S3 bucket
            authorization = """
        iam_role '{0}'
        """.format(aws_role)
        else:
            authorization = ""

    s3_to_sql = """
       copy {0}
       from '{1}'
       delimiter '{2}'
       ignoreheader 1
       csv quote as '{3}'
       dateformat '{4}'
       timeformat '{5}'
       {6}
       {7}
       """.format(redshift_table_name, bucket_name, delimiter, quotechar, dateformat,
                  timeformat, authorization, parameters)
    logger.info(f"Copy sql:{s3_to_sql}")
    if region:
        s3_to_sql = s3_to_sql + "region '{0}'".format(region)
    if aws_token != '':
        s3_to_sql = s3_to_sql + "\n\tsession_token '{0}'".format(aws_token)
    s3_to_sql = s3_to_sql + ';'
    if verbose:
        logger.info(mask_aws_credentials(s3_to_sql))
        # send the file
        logger.info('FILLING THE TABLE IN REDSHIFT')
    try:
        cursor.execute(s3_to_sql)
        connect.commit()
    except Exception as e:
        print(f"Error during execution of query {s3_to_sql}: {e}")
        logger.error(e)
        traceback.print_exc(file=sys.stdout)
        connect.rollback()
        raise


def _date_converter(ts):
    """Detects current_date variable and evaluates it, very simple and for
    the use case of daily upload.
    Expected input:
    "current_date  +  '18:00-00'::TIMETZ - interval '1 day'"""
    if 'current_date' in ts:
        from datetime import date, timedelta
        today = date.today()
        interval_idx = ts.find('interval')
        day_idx = ts.find('day')
        # if both are present
        if interval_idx != -1 and day_idx != -1:
            # find the number of the days in the rest of the string, expecting the only one present
            digit = int(''.join(filter(str.isdigit, ts[interval_idx:-1])))
            ts = today + timedelta(days=-digit)
        else:
            ts = today
    return ts


def pandas_to_redshift(data_frame,
                       redshift_table_name,
                       ts_start,
                       ts_end,
                       rs_iam_role="",  # IAM role, used for copy upload operation by redshift cluster
                       column_data_types=None,
                       index=False,
                       save_local=False,
                       delimiter=',',
                       quotechar='"',
                       dateformat='auto',
                       timeformat='auto',
                       region='',
                       append=False,
                       diststyle='even',
                       distkey='',
                       sort_interleaved=False,
                       sortkey='',
                       parameters='',
                       verbose=True,
                       # explicit names for columns, which will be converted to "SUPER" format in redshift
                       json_columns=None,
                       **kwargs):
    # Validate column names.
    data_frame = validate_column_names(data_frame)
    # query to grab 1 raw from data table
    get_schema_sql = f'select * from {redshift_table_name} limit 1'

    schema_df = None
    if append:
        schema_df = redshift_to_pandas(get_schema_sql)

    data_frame = invalidate_to_schema(data_frame, schema_df)

    # Send data to S3
    # csv_name = '{}-{}.csv'.format(redshift_table_name, uuid.uuid4())
    csv_name = '{}-{}_{}.csv'.format(redshift_table_name, _date_converter(ts_start),
                                     _date_converter(ts_end))
    s3_kwargs = {k: v for k, v in kwargs.items()
                 if k in S3_ACCEPTED_KWARGS and v is not None}
    df_to_s3(data_frame, csv_name, index, save_local, delimiter, verbose=verbose, **s3_kwargs)

    # CREATE AN EMPTY TABLE IN REDSHIFT
    if not append:
        create_redshift_table(data_frame, redshift_table_name,
                              column_data_types, index, append,
                              diststyle, distkey, sort_interleaved, sortkey, json_columns, verbose=verbose)

    # CREATE THE COPY STATEMENT TO SEND FROM S3 TO THE TABLE IN REDSHIFT
    s3_to_redshift(redshift_table_name, csv_name, rs_iam_role, delimiter, quotechar,
                   dateformat, timeformat, region, parameters, verbose=verbose)


def exec_commit(sql_query):
    cursor.execute(sql_query)
    connect.commit()


def close_up_shop():
    global connect, cursor, s3, s3_bucket_var, s3_subdirectory_var, aws_1, aws_2, aws_token
    cursor.close()
    connect.commit()
    connect.close()
    try:
        del connect, cursor
    except:
        pass
    try:
        del s3, s3_bucket_var, s3_subdirectory_var, aws_1, aws_2, aws_token
    except:
        pass

# -------------------------------------------------------------------------------
