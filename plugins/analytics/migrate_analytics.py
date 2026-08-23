#!/usr/bin/env python3
"""迁移 analytics 数据：SQLite → PostgreSQL（完整幂等版本）
注意：sqlite3 仅在执行 migrate() 时按需导入，顶层导入不再依赖 sqlite3。
"""
import psycopg2, psycopg2.extras, sys, os

# 通过环境变量配置（部署时注入），保留默认占位值
SQLITE = os.environ.get('ANALYTICS_SQLITE_PATH', '/path/to/analytics.db')
PG_DSN = os.environ.get(
    'ANALYTICS_PG_DSN',
    'host=localhost port=5432 dbname=appdb user=app password=your_password'
)
# 目标 PG schema（默认为 analytics，可通过环境变量覆盖，避免与 schema 名耦合）
SCHEMA = os.environ.get('ANALYTICS_SCHEMA', 'analytics')
BATCH = 500

TABLES = [
    # (table, cols, conflict_cols_or_None, sqlite_query)
    ('analytics_geo_stats', ['date','country','city','pv','uv'],
     ['date','country','city'],
     'SELECT date, country, city, pv, uv FROM analytics_geo_stats'),

    ('analytics_page_stats', ['date','path','pv','uv','unique_entries','unique_exits','avg_time_on_page','exit_rate','total_time'],
     ['date','path'],
     'SELECT date, path, pv, uv, unique_entries, unique_exits, avg_time_on_page, exit_rate, total_time FROM analytics_page_stats'),

    ('analytics_device_stats', ['date','device_type','browser','os_name','pv','uv'],
     ['date','device_type','browser','os_name'],
     'SELECT date, device_type, browser, os_name, pv, uv FROM analytics_device_stats'),

    ('analytics_source_stats', ['date','source_type','source_name','pv','uv'],
     ['date','source_type','source_name'],
     'SELECT date, source_type, source_name, pv, uv FROM analytics_source_stats'),

    ('analytics_daily_stats', ['date','pv','uv','ipv','new_visitors','returning_visitors','bounce_rate','avg_session_duration','avg_depth','bot_pv','error_pv','avg_response_time','total_sessions','peak_concurrent','peak_concurrent_time','last_calculated'],
     ['date'],
     'SELECT date, pv, uv, ipv, new_visitors, returning_visitors, bounce_rate, avg_session_duration, avg_depth, bot_pv, error_pv, avg_response_time, total_sessions, peak_concurrent, peak_concurrent_time, last_calculated FROM analytics_daily_stats'),

    ('analytics_hourly_stats', ['date','hour','service_name','pv','uv','ipv','new_visitors','bounce_count','total_time','session_count','bot_count','error_count','avg_response_time'],
     ['date','hour','service_name'],
     'SELECT date, hour, service_name, pv, uv, ipv, new_visitors, bounce_count, total_time, session_count, bot_count, error_count, avg_response_time FROM analytics_hourly_stats'),

    ('analytics_alerts', ['name','metric','operator','threshold','time_window','enabled','channels','created_at','updated_at'],
     None,
     'SELECT name, metric, operator, threshold, time_window, enabled, channels, created_at, updated_at FROM analytics_alerts'),

    ('analytics_privacy_config', ['key','value','updated_at'],
     ['key'],
     'SELECT key, value, updated_at FROM analytics_privacy_config'),

    ('analytics_events', ['timestamp','visitor_hash','event_name','event_category','event_label','event_value','path','service_name','metadata'],
     None,
     'SELECT timestamp, visitor_hash, event_name, event_category, event_label, event_value, path, service_name, metadata FROM analytics_events'),

    ('analytics_visitor_sessions', ['session_hash','visitor_hash','date','start_time','end_time','duration','page_views','entry_path','exit_path','referer','browser','os_name','device_type','country','city','is_bot','is_new_visitor'],
     ['session_hash'],
     'SELECT session_hash, visitor_hash, date, start_time, end_time, duration, page_views, entry_path, exit_path, referer, browser, os_name, device_type, country, city, is_bot, is_new_visitor FROM analytics_visitor_sessions'),

    ('analytics_logs', ['timestamp','visitor_hash','session_hash','ip_prefix','country','city','user_agent','browser','browser_version','os_name','device_type','is_bot','path','query_string','referer','referer_domain','utm_source','utm_medium','utm_campaign','language','status_code','response_time','request_method','service_name','full_url','content_type'],
     None,
     'SELECT timestamp, visitor_hash, session_hash, ip_prefix, country, city, user_agent, browser, browser_version, os_name, device_type, is_bot, path, query_string, referer, referer_domain, utm_source, utm_medium, utm_campaign, language, status_code, response_time, request_method, service_name, full_url, content_type FROM analytics_logs'),
]

def build_sql(table, cols, conflict_cols):
    cols_s = ','.join(cols)
    sql = f'INSERT INTO {SCHEMA}.{table} ({cols_s}) VALUES %s'
    if conflict_cols:
        sql += f' ON CONFLICT ({",".join(conflict_cols)}) DO NOTHING'
    return sql

def migrate():
    if not os.path.exists(SQLITE):
        print(f'ERROR: {SQLITE} not found')
        return 1

    # 按需导入 sqlite3（仅执行迁移时需要，缺失时给出明确错误而非顶层导入崩溃）
    try:
        import sqlite3
    except ImportError:
        print('ERROR: sqlite3 module is required for SQLite → PostgreSQL migration')
        return 1

    sl = sqlite3.connect(SQLITE)
    sl.row_factory = sqlite3.Row
    pg = psycopg2.connect(PG_DSN)
    pg.autocommit = False

    total_ok = 0

    for table, cols, conflict_cols, query in TABLES:
        try:
            cnt = sl.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        except Exception:
            continue
        if cnt == 0:
            continue

        print(f'  [{table}] {cnt} rows...', end=' ', flush=True)
        rows = sl.execute(query).fetchall()
        values = [tuple(r[c] for c in cols) for r in rows]
        sql = build_sql(table, cols, conflict_cols)

        cur = pg.cursor()
        ok = 0
        for i in range(0, len(values), BATCH):
            batch = values[i:i+BATCH]
            try:
                psycopg2.extras.execute_values(cur, sql, batch, page_size=len(batch))
                ok += len(batch)
            except Exception as e:
                print(f'\n    BATCH ERROR: {e}')
                cur2 = pg.cursor()
                for row in batch:
                    try:
                        cur2.execute(
                            f'INSERT INTO {SCHEMA}.{table} ({",".join(cols)}) VALUES ({",".join(["%s"]*len(cols))}) ON CONFLICT DO NOTHING',
                            row
                        )
                    except Exception:
                        pass
                pg.commit()
        pg.commit()
        total_ok += ok
        print(f'OK')

    sl.close()
    pg.close()
    print(f'\nDONE: {total_ok} rows total')
    return 0

if __name__ == '__main__':
    sys.exit(migrate())
