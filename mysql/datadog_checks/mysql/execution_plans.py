import json
from contextlib import closing

import pymysql

import datadog_agent
from datadog_checks.base import is_affirmative

from datadog_checks.base.utils.db.statement_metrics import is_dbm_enabled
from datadog_checks.base.utils.db.sql import compute_sql_signature, compute_exec_plan_signature, submit_exec_plan_events


VALID_EXPLAIN_STATEMENTS = frozenset({
  'select',
  'table',
  'delete',
  'insert',
  'replace',
  'update',
})


class RetryableError(Exception):
    """Error to raise when a retryable error occurs"""


class NonRetryableError(Exception):
    """Error to raise when a non-retryable error occurs"""


class ExecutionPlansMixin(object):
    """
    Mixin for collecting execution plans from query samples. Where defined, the user will attempt
    to use the stored procedure `explain_statement` which allows collection of execution plans
    using the permissions of the procedure definer.
    """

    def __init__(self, *args, **kwargs):
        # TODO: Make this a configurable limit
        self.query_limit = 500
        self._checkpoint = None
        self._auto_enable_eshl = None
        # For each schema, keep track of which methods work to collect execution plans
        self._explain_functions_by_schema = {}
    
    def _enable_performance_schema_consumers(self, db):
        query = """UPDATE performance_schema.setup_consumers SET enabled = 'YES' WHERE name = 'events_statements_history_long'"""
        with closing(db.cursor()) as cursor:
            try:
                cursor.execute(query)
            except pymysql.err.OperationalError as e:
                if e.args[0] == 1142:
                    self.log.error('Unable to create performance_schema consumers: %s', e.args[1])
                else:
                    raise
            except pymysql.err.InternalError as e:
                if e.args[0] == 1290:
                    self.log.warning('Unable to create performance_schema consumers because the instance is read-only')
                    self._auto_enable_eshl = False
                else:
                    raise
            else:
                self.log.info('Successfully enabled events_statements_history_long consumers')

    def _collect_execution_plans(self, db, tags, options):
        if self._auto_enable_eshl is None:
            self._auto_enable_eshl = is_affirmative(options.get('auto_enable_events_statements_history_long', False))
        if not (is_dbm_enabled() and is_affirmative(options.get('collect_execution_plans', True))):
            return False

        tags = list(set(self.service_check_tags + tags))
        if self._checkpoint is None:
            with closing(db.cursor()) as cursor:
                cursor.execute('SELECT MAX(timer_start) FROM performance_schema.events_statements_history_long')
                result = cursor.fetchone()
            if not result or not all(result):
                self.log.debug('Unable to fetch from performance_schema.events_statements_history_long')
                if self._auto_enable_eshl:
                    self._enable_performance_schema_consumers(db)
                return False
            self._checkpoint = result[0]
        # Select the most recent events with a bias towards events which have higher wait times
        query = """
            SELECT current_schema AS current_schema,
                   sql_text AS sql_text,
                   IFNULL(digest_text, sql_text) AS digest_text,
                   timer_start AS timer_start,
                   MAX(timer_wait) / 1000 AS max_timer_wait_ns,
                   lock_time / 1000 AS lock_time_ns,
                   rows_affected,
                   rows_sent,
                   rows_examined,
                   select_full_join,
                   select_full_range_join,
                   select_range,
                   select_range_check,
                   select_scan,
                   sort_merge_passes,
                   sort_range,
                   sort_rows,
                   sort_scan,
                   no_index_used,
                   no_good_index_used
              FROM performance_schema.events_statements_history_long
             WHERE sql_text IS NOT NULL
               AND event_name like %s
               AND digest_text NOT LIKE %s
               AND timer_start > %s
          GROUP BY digest
          ORDER BY timer_wait DESC
              LIMIT %s
            """

        with closing(db.cursor(pymysql.cursors.DictCursor)) as cursor:
            cursor.execute(query, ('statement/%', 'EXPLAIN %', self._checkpoint, self.query_limit))
            rows = cursor.fetchall()
            cursor.execute('SET @@SESSION.sql_notes = 0')

        events = []
        num_truncated = 0

        for row in rows:
            if not row or not all(row):
                self.log.debug('Row was unexpectedly truncated or events_statements_history_long table is not enabled')
                continue
            schema = row['current_schema']
            sql_text = row['sql_text']
            digest_text = row['digest_text']
            self._checkpoint = max(row['timer_start'], self._checkpoint)
            duration_ns = row['max_timer_wait_ns']

            if not sql_text:
                continue

            # The SQL_TEXT column will store 1024 chars by default. Plans cannot be captured on truncated
            # queries, so the `performance_schema_max_sql_text_length` variable must be raised.
            if sql_text[-3:] == '...':
                num_truncated += 1
                continue

            with closing(db.cursor()) as cursor:
                plan = self._attempt_explain(cursor, sql_text, schema)
                normalized_plan = datadog_agent.obfuscate_sql_exec_plan(plan, normalize=True) if plan else None
                obfuscated_statement = datadog_agent.obfuscate_sql(sql_text)
                if plan:
                    events.append({
                        'duration': duration_ns,
                        'db': {
                            'instance': schema,
                            'statement': obfuscated_statement,
                            'query_signature': compute_sql_signature(obfuscated_statement),
                            'plan': plan,
                            'plan_cost': self._parse_execution_plan_cost(plan),
                            'plan_signature': compute_exec_plan_signature(normalized_plan),
                            'debug': {
                                'normalized_plan': normalized_plan,
                                'obfuscated_plan': datadog_agent.obfuscate_sql_exec_plan(plan),
                                'digest_text': digest_text,
                            },
                            'mysql': {
                                'lock_time': row['lock_time_ns'],
                                'rows_affected': row['rows_affected'],
                                'rows_sent': row['rows_sent'],
                                'rows_examined': row['rows_examined'],
                                'select_full_join': row['select_full_join'],
                                'select_full_range_join': row['select_full_range_join'],
                                'select_range': row['select_range'],
                                'select_range_check': row['select_range_check'],
                                'select_scan': row['select_scan'],
                                'sort_merge_passes': row['sort_merge_passes'],
                                'sort_range': row['sort_range'],
                                'sort_rows': row['sort_rows'],
                                'sort_scan': row['sort_scan'],
                                'no_index_used': row['no_index_used'],
                                'no_good_index_used': row['no_good_index_used'],
                            }
                        }
                    })

        submit_exec_plan_events(events, tags, "mysql")
        if num_truncated > 0:
            self.log.warning(
                'Unable to collect %d/%d execution plans due to truncated SQL text. Consider raising '
                '`performance_schema_max_sql_text_length` to capture these queries.',
                num_truncated,
                num_truncated + len(events)
            )

    def _attempt_explain(self, cursor, statement, schema):
        """
        Tries the available methods used to explain a statement for the given schema. If a non-retryable
        error occurs (such as a permissions error), then statements executed under the schema will be
        disallowed in future attempts.
        """
        plan = None

        if not self._can_explain(statement):
            return None

        if self._explain_functions_by_schema.get(schema) is False:
            # Schema has no available functions to try
            return None

        # Switch to the right schema
        try:
            self._use_schema(cursor, schema)
        except NonRetryableError:
            self._explain_functions_by_schema[schema] = False
            return None
        except RetryableError:
            return None

        if schema in self._explain_functions_by_schema:
            plan = self._explain_functions_by_schema[schema](cursor, statement)
        else:
            for explain_function in (self._run_explain_procedure, self._run_explain):
                try:
                    plan = explain_function(cursor, statement)
                    self._explain_functions_by_schema[schema] = explain_function
                    break
                except NonRetryableError:
                    self._explain_functions_by_schema[schema] = False
                    continue
                except RetryableError:
                    continue

        return plan

    def _use_schema(self, cursor, schema):
        try:
            if schema is not None:
                cursor.execute('USE `{}`'.format(schema))
        except (pymysql.err.InternalError, pymysql.err.ProgrammingError) as e:
            if len(e.args) != 2:
                raise
            if e.args[0] == 1049:
                # Unknown database
                raise NonRetryableError(*e.args)
            elif e.args[0] == 1044:
                # Access denied on database
                raise NonRetryableError(*e.args)
            else:
                raise RetryableError(*e.args) from e

    def _run_explain(self, cursor, statement):
        """
        Run the explain using the EXPLAIN statement
        """
        try:
            cursor.execute('EXPLAIN FORMAT=json {statement}'.format(statement=statement))
            self.log.debug('Successfully ran explain using EXPLAIN statement: %s', statement)
        except (pymysql.err.InternalError, pymysql.err.ProgrammingError) as e:
            if len(e.args) != 2:
                raise
            if e.args[0] == 1046:
                # No permission on statement
                self.log.warning('Failed to collect EXPLAIN due to a permissions error: %s, Statement: %s', e.args, statement)
                raise NonRetryableError(*e.args)
            elif e.args[0] == 1064:
                # Programming error; retryable because it may be due to the statement being explained
                self.log.warning('Programming error when collecting EXPLAIN: %s, Statement: %s', e.args, statement)
                raise RetryableError(*e.args)
            else:
                raise RetryableError(*e.args) from e

        return cursor.fetchone()[0]

    def _run_explain_procedure(self, cursor, statement):
        """
        Run the explain by calling the stored procedure `explain_statement`.
        """
        try:
            cursor.execute('CALL explain_statement(%s)', statement)
            self.log.debug('Successfully ran explain using explain_statement procedure: %s', statement)
        except (pymysql.err.InternalError, pymysql.err.ProgrammingError) as e:
            if len(e.args) != 2:
                raise
            if e.args[0] == 1370:
                # No execute
                raise NonRetryableError(*e.args)
            elif e.args[0] == 1305:
                # Procedure does not exist
                raise NonRetryableError(*e.args)
            else:
                raise RetryableError(*e.args) from e
        return cursor.fetchone()[0]

    @staticmethod
    def _can_explain(statement):
        # TODO: cleaner query cleaning to strip comments, etc.
        return statement.strip().split(' ', 1)[0].lower() in VALID_EXPLAIN_STATEMENTS

    @staticmethod
    def _parse_execution_plan_cost(execution_plan):
        """
        Parses the total cost from the execution plan, if set. If not set, returns cost of 0.
        """
        cost = json.loads(execution_plan).get('query_block', {}).get('cost_info', {}).get('query_cost', 0.)
        return float(cost or 0.)
