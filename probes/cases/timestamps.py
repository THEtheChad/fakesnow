"""Dates and timestamps: DATE_TRUNC, the three timestamp types, and date functions. The run sets TIMEZONE to UTC."""

from probes import UNFILED, Case, Compare, Expected
from probes.cases._gaps import DUPLICATE_NAMES, ERROR_CODES

TYPES_ONLY = Compare(rows=False)
TZ_OFFSET = "fakesnow's TIMESTAMP_TZ is DuckDB's TIMESTAMPTZ, which keeps only the UTC instant, so the offset is lost"

CASES = [
    # --- DATE_TRUNC
    Case(
        id="timestamps.date-trunc-month-date",
        description="DATE_TRUNC('month', <date>) is a DATE",
        refs=["#471"],
        sql="select date_trunc('month', '2024-05-09'::date)",
    ),
    Case(
        id="timestamps.date-trunc-hour-date",
        description="DATE_TRUNC('hour', <date>) is a DATE",
        refs=["#471"],
        sql="select date_trunc('hour', '2024-05-09'::date)",
    ),
    Case(
        id="timestamps.trunc-date-month",
        description="TRUNC(<date>, 'month') is a DATE",
        refs=["#471"],
        sql="select trunc('2024-05-09'::date, 'month')",
    ),
    Case(
        id="timestamps.date-trunc-null",
        description="DATE_TRUNC of a bare NULL",
        refs=["#471"],
        sql="select date_trunc('month', null)",
        expected=Expected(["#471"], "Snowflake types DATE_TRUNC of a bare NULL as TEXT (2), fakesnow as DATE (3)"),
    ),
    Case(
        id="timestamps.date-trunc-string",
        description="DATE_TRUNC of a string literal",
        refs=["#471"],
        sql="select date_trunc('month', '2024-05-09')",
        expected=ERROR_CODES,
    ),
    Case(
        id="timestamps.date-trunc-timestamp",
        description="DATE_TRUNC of a TIMESTAMP_NTZ keeps the type",
        sql="select date_trunc('day', '2024-05-09 10:11:12'::timestamp_ntz), date_trunc('week', '2024-05-09'::date)",
    ),
    # --- timestamp types
    Case(
        id="timestamps.localtimestamp-type",
        description="LOCALTIMESTAMP is a TIMESTAMP_LTZ like CURRENT_TIMESTAMP",
        refs=["#480"],
        compare=TYPES_ONLY,
        sql="select localtimestamp as l, current_timestamp as c",
        expected=Expected(["#480"], "fakesnow's LOCALTIMESTAMP is a TIMESTAMP_NTZ (8)"),
    ),
    Case(
        id="timestamps.ltz-tz-ntz-casts",
        description="Casting a string to TIMESTAMP_LTZ, TIMESTAMP_TZ and TIMESTAMP_NTZ",
        refs=["#474"],
        sql="select '2026-10-26 01:02:03'::timestamp_ltz as l, '2026-10-26 01:02:03'::timestamp_tz as z, "
        "'2026-10-26 01:02:03'::timestamp_ntz as n",
    ),
    Case(
        id="timestamps.ltz-tz-columns",
        description="Result types of TIMESTAMP_LTZ and TIMESTAMP_TZ columns",
        refs=["#474"],
        setup=["create or replace table ts_ltzt (l timestamp_ltz, z timestamp_tz)"],
        sql="select * from ts_ltzt",
    ),
    Case(
        id="timestamps.ltz-information-schema",
        description="INFORMATION_SCHEMA.COLUMNS data types of TIMESTAMP_LTZ and TIMESTAMP_TZ columns",
        refs=["#474"],
        setup=["create or replace table ts_ltzt (l timestamp_ltz, z timestamp_tz)"],
        sql="select column_name, data_type from information_schema.columns "
        "where table_schema = '{schema}' and table_name = 'TS_LTZT' order by ordinal_position",
    ),
    Case(
        id="timestamps.current-time-functions",
        description="Types of SYSDATE, GETDATE, SYSTIMESTAMP, CURRENT_DATE and CURRENT_TIME",
        refs=["#474"],
        compare=TYPES_ONLY,
        sql="select sysdate() as s, getdate() as g, systimestamp() as t, current_date() as d, current_time() as tm",
    ),
    Case(
        id="timestamps.null-timestamps",
        description="Result types of NULL cast to each timestamp type",
        refs=["#465"],
        sql="select null::timestamp_ntz, null::timestamp_ltz, null::timestamp_tz",
        expected=DUPLICATE_NAMES,
    ),
    Case(
        id="timestamps.tz-offset-kept",
        description="A TIMESTAMP_TZ keeps its offset (compared without normalising aware datetimes to instants)",
        compare=Compare(normalise=False),
        sql="select '2024-01-01 10:00:00 +02:00'::timestamp_tz as t",
        expected=Expected([UNFILED], TZ_OFFSET),
    ),
    Case(
        id="timestamps.tz-to-varchar",
        description="A TIMESTAMP_TZ cast to VARCHAR shows its offset in Snowflake's default format",
        sql="select to_varchar('2024-01-01 10:00:00 +02:00'::timestamp_tz) as t",
        expected=Expected([UNFILED], TZ_OFFSET),
    ),
    Case(
        id="timestamps.ntz-fraction",
        description="TIMESTAMP_NTZ with nanoseconds comes back with microseconds",
        sql="select '2024-01-01 10:00:00.123456789'::timestamp_ntz",
    ),
    Case(
        id="timestamps.convert-timezone",
        description="CONVERT_TIMEZONE of a TIMESTAMP_NTZ, with source and target zones",
        sql="select convert_timezone('UTC', 'America/New_York', '2024-01-01 12:00:00'::timestamp_ntz)",
    ),
    Case(
        id="timestamps.timestamp-plus-interval",
        description="Adding an INTERVAL to a TIMESTAMP_NTZ and a DATE",
        sql="select '2024-01-01 00:00:00'::timestamp_ntz + interval '1 day', '2024-01-31'::date + interval '1 month'",
        expected=Expected([UNFILED], "DATE + INTERVAL is a TIMESTAMP_NTZ in fakesnow, a DATE in Snowflake"),
    ),
    # --- date functions
    Case(
        id="timestamps.dateadd",
        description="DATEADD by day, month (end of month) and hour",
        sql="select dateadd(day, 1, '2024-01-31'::date), dateadd(month, 1, '2024-01-31'::date), "
        "dateadd(hour, 1, '2024-01-01 00:00:00'::timestamp_ntz)",
    ),
    Case(
        id="timestamps.dateadd-string",
        description="DATEADD on a date string",
        sql="select dateadd(day, 1, '2024-01-31')",
    ),
    Case(
        id="timestamps.datediff",
        description="DATEDIFF counts boundaries crossed, not elapsed time",
        sql="select datediff(day, '2024-01-01'::date, '2024-03-01'::date), "
        "datediff(month, '2024-01-31'::date, '2024-02-01'::date), "
        "datediff(year, '2023-12-31'::date, '2024-01-01'::date), "
        "datediff(hour, '2024-01-01 10:59:00'::timestamp_ntz, '2024-01-01 11:00:00'::timestamp_ntz)",
    ),
    Case(
        id="timestamps.last-day",
        description="LAST_DAY of the month and of the year",
        sql="select last_day('2024-02-10'::date), last_day('2024-02-10'::date, 'year')",
    ),
    Case(
        id="timestamps.to-date-formats",
        description="TO_DATE with no format, DD/MM/YYYY and YYYYMMDD",
        sql="select to_date('2024-05-09'), to_date('09/05/2024', 'DD/MM/YYYY'), to_date('20240509', 'YYYYMMDD')",
    ),
    Case(
        id="timestamps.to-date-invalid",
        description="TO_DATE of a string that isn't a date fails",
        sql="select to_date('not a date')",
        expected=ERROR_CODES,
    ),
    Case(
        id="timestamps.try-to-date",
        description="TRY_TO_DATE of a string that isn't a date is NULL",
        sql="select try_to_date('not a date'), try_to_date('2024-05-09')",
    ),
    Case(
        id="timestamps.to-timestamp-formats",
        description="TO_TIMESTAMP with no format and with DD/MM/YYYY HH24:MI",
        sql="select to_timestamp('2024-05-09 10:11:12'), to_timestamp('09/05/2024 10:11', 'DD/MM/YYYY HH24:MI')",
    ),
    Case(
        id="timestamps.date-parts",
        description="YEAR, MONTH, DAY, DAYOFWEEK and DAYOFYEAR of a date",
        sql="select year(d), month(d), day(d), dayofweek(d), dayofyear(d), quarter(d) "
        "from (select '2024-05-09'::date as d)",
    ),
    Case(
        id="timestamps.extract-epoch",
        description="EXTRACT of hour and epoch seconds from a TIMESTAMP_NTZ",
        sql="select extract(hour from '2024-05-09 10:11:12'::timestamp_ntz), "
        "extract(epoch_second from '2024-05-09 10:11:12'::timestamp_ntz)",
    ),
    Case(
        id="timestamps.to-char-formats",
        description="TO_CHAR of a date and a timestamp with formats",
        sql="select to_char('2024-05-09'::date, 'YYYY-MM-DD'), "
        "to_char('2024-05-09 10:11:12'::timestamp_ntz, 'HH24:MI'), to_char('2024-05-09'::date, 'DD Mon YYYY')",
        expected=Expected([UNFILED], "TO_CHAR's Mon format element isn't translated"),
    ),
    Case(
        id="timestamps.date-to-varchar-default",
        description="A date and a TIMESTAMP_NTZ cast to VARCHAR in the default output format",
        sql="select '2024-05-09'::date::varchar, '2024-05-09 10:11:12'::timestamp_ntz::varchar",
        expected=Expected([UNFILED], "a TIMESTAMP_NTZ cast to VARCHAR lacks Snowflake's default milliseconds ('.000')"),
    ),
]
