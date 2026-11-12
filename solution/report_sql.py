"""SQL for reports. Everything is built as TEMP objects per run; the ledger file is never written.

Chain:  r_direct -> r_call_lead -> r_linked -> r_calls -> r_valid_disp -> r_call_disp
        -> r_lead_stats / r_lead_current / r_daily.
Linking, orphan status, calls and effective dispositions are all derived here from the stored raw
facts, so they depend on nothing but the ledger contents (not on ingest order or drop boundaries).
"Latest" is picked with NOT EXISTS anti-joins (no window functions) to stay SQLite-portable.
IST is UTC+5:30 = 19800 seconds.
"""

SETUP: list[str] = [
    # Events that link to a lead by (workflow, phone hash). Overrides never link this way.
    """CREATE TEMP VIEW r_direct AS
       SELECT e.seq, e.event_type, e.workflow_id, e.call_id, l.lead_id
       FROM events e
       JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_hash = e.phone_hash
       WHERE e.event_type <> 'disposition.override'""",

    # One lead per call: the lead of its call.initiated if any, else the lowest lead_id seen.
    """CREATE TEMP TABLE r_call_lead AS
       SELECT workflow_id, call_id,
              COALESCE(MIN(CASE WHEN event_type = 'call.initiated' THEN lead_id END), MIN(lead_id)) AS lead_id
       FROM r_direct
       WHERE call_id IS NOT NULL
       GROUP BY workflow_id, call_id""",
    "CREATE UNIQUE INDEX temp.ix_rcl ON r_call_lead (workflow_id, call_id)",

    # Every linked (non-orphan) event. Overrides link through their call_id.
    """CREATE TEMP TABLE r_linked AS
       SELECT seq, event_type, workflow_id, call_id, lead_id FROM r_direct
       UNION ALL
       SELECT e.seq, e.event_type, e.workflow_id, e.call_id, cl.lead_id
       FROM events e
       JOIN r_call_lead cl ON cl.workflow_id = e.workflow_id AND cl.call_id = e.call_id
       WHERE e.event_type = 'disposition.override'""",
    "CREATE UNIQUE INDEX temp.ix_rl_seq ON r_linked (seq)",
    "CREATE INDEX temp.ix_rl_call ON r_linked (workflow_id, call_id)",

    # A call exists only if a linked call.initiated for the call's lead exists.
    """CREATE TEMP TABLE r_calls AS
       SELECT cl.workflow_id, cl.call_id, cl.lead_id,
              MIN(e.occurred_at_utc) AS initiated_at,
              EXISTS (SELECT 1 FROM r_linked x
                      WHERE x.workflow_id = cl.workflow_id AND x.call_id = cl.call_id
                        AND x.lead_id = cl.lead_id AND x.event_type = 'call.connected') AS connected,
              EXISTS (SELECT 1 FROM r_linked x
                      WHERE x.workflow_id = cl.workflow_id AND x.call_id = cl.call_id
                        AND x.lead_id = cl.lead_id AND x.event_type = 'call.telephony_failed') AS failed
       FROM r_call_lead cl
       JOIN r_linked i ON i.workflow_id = cl.workflow_id AND i.call_id = cl.call_id
                      AND i.lead_id = cl.lead_id AND i.event_type = 'call.initiated'
       JOIN events e ON e.seq = i.seq
       GROUP BY cl.workflow_id, cl.call_id, cl.lead_id""",
    "CREATE UNIQUE INDEX temp.ix_rc_call ON r_calls (workflow_id, call_id)",
    "CREATE INDEX temp.ix_rc_lead ON r_calls (lead_id)",

    # Disposition events with a catalogue code, on counted calls. grp 0 = override, 1 = set.
    """CREATE TEMP TABLE r_valid_disp AS
       SELECT e.workflow_id, e.call_id, e.event_id, e.occurred_at_utc AS t,
              CASE e.event_type WHEN 'disposition.override' THEN 0 ELSE 1 END AS grp,
              d.code, d.is_terminal, d.is_success, c.lead_id
       FROM r_linked l
       JOIN events e ON e.seq = l.seq
       JOIN dispositions d ON d.workflow_id = e.workflow_id AND d.code = e.disposition_code
       JOIN r_calls c ON c.workflow_id = e.workflow_id AND c.call_id = e.call_id AND c.lead_id = l.lead_id
       WHERE e.event_type IN ('disposition.set', 'disposition.override')""",
    "CREATE INDEX temp.ix_rvd_call ON r_valid_disp (workflow_id, call_id)",

    # Linked disposition events whose code is missing or not in the catalogue.
    """CREATE TEMP VIEW r_unknown_disp AS
       SELECT e.seq
       FROM r_linked l
       JOIN events e ON e.seq = l.seq
       WHERE e.event_type IN ('disposition.set', 'disposition.override')
         AND NOT EXISTS (SELECT 1 FROM dispositions d
                         WHERE d.workflow_id = e.workflow_id AND d.code = e.disposition_code)""",

    # Effective disposition per call: latest valid override, else latest valid set.
    # Order: override before set, then latest occurred_at, then highest event_id.
    """CREATE TEMP TABLE r_call_disp AS
       SELECT v.workflow_id, v.call_id, v.lead_id, v.code, v.is_terminal, v.is_success
       FROM r_valid_disp v
       WHERE NOT EXISTS (
           SELECT 1 FROM r_valid_disp w
           WHERE w.workflow_id = v.workflow_id AND w.call_id = v.call_id
             AND (w.grp < v.grp
                  OR (w.grp = v.grp AND (w.t > v.t OR (w.t = v.t AND w.event_id > v.event_id)))))""",
    "CREATE UNIQUE INDEX temp.ix_rcd_call ON r_call_disp (workflow_id, call_id)",

    """CREATE TEMP VIEW r_lead_stats AS
       SELECT l.lead_id, l.workflow_id, l.batch_id, l.external_ref, l.phone_last4,
              COUNT(c.call_id) AS total_calls,
              COALESCE(SUM(c.connected), 0) AS connected_calls,
              MAX(c.initiated_at) AS last_call_at,
              COALESCE(MAX(d.is_success), 0) AS ever_success,
              CASE WHEN COUNT(c.call_id) = 0 THEN 'yet_to_reach_out'
                   WHEN COALESCE(MAX(d.is_terminal), 0) = 1 THEN 'completed'
                   WHEN MIN(c.failed) = 1 THEN 'in_progress_telephony_failed'
                   ELSE 'in_progress_dialed' END AS bucket
       FROM leads l
       LEFT JOIN r_calls c ON c.lead_id = l.lead_id
       LEFT JOIN r_call_disp d ON d.workflow_id = c.workflow_id AND d.call_id = c.call_id
       GROUP BY l.lead_id""",

    # Disposition of the lead's most recent call (by initiated time, then call_id) that has one.
    """CREATE TEMP VIEW r_lead_current AS
       SELECT c.lead_id, d.code
       FROM r_calls c
       JOIN r_call_disp d ON d.workflow_id = c.workflow_id AND d.call_id = c.call_id
       WHERE NOT EXISTS (
           SELECT 1
           FROM r_calls c2
           JOIN r_call_disp d2 ON d2.workflow_id = c2.workflow_id AND d2.call_id = c2.call_id
           WHERE c2.lead_id = c.lead_id
             AND (c2.initiated_at > c.initiated_at
                  OR (c2.initiated_at = c.initiated_at AND c2.call_id > c.call_id)))""",

    """CREATE TEMP VIEW r_daily AS
       SELECT c.workflow_id,
              date(c.initiated_at + 19800, 'unixepoch') AS day,
              COUNT(*) AS total_calls,
              SUM(c.connected) AS connected_calls,
              COUNT(DISTINCT CASE WHEN d.is_success = 1 THEN c.lead_id END) AS success_leads
       FROM r_calls c
       LEFT JOIN r_call_disp d ON d.workflow_id = c.workflow_id AND d.call_id = c.call_id
       GROUP BY c.workflow_id, day""",
]

Q_BATCH_SUMMARY = """
    SELECT workflow_id, batch_id, COUNT(*),
           SUM(bucket = 'yet_to_reach_out'), SUM(bucket = 'in_progress_telephony_failed'),
           SUM(bucket = 'in_progress_dialed'), SUM(bucket = 'completed')
    FROM r_lead_stats
    GROUP BY workflow_id, batch_id
    ORDER BY workflow_id, batch_id"""

Q_DAILY = """
    SELECT workflow_id, day, total_calls, connected_calls, success_leads
    FROM r_daily
    ORDER BY workflow_id, day"""

Q_LEAD_REPORT = """
    SELECT s.workflow_id, s.external_ref, s.batch_id, s.phone_last4, s.bucket, s.total_calls,
           s.connected_calls, s.last_call_at, c.code, s.ever_success
    FROM r_lead_stats s
    LEFT JOIN r_lead_current c ON c.lead_id = s.lead_id
    ORDER BY s.workflow_id, s.external_ref, s.lead_id"""

Q_REJECTIONS = """
    SELECT f.workflow_id, f.batch_id, r.row_number, r.reason
    FROM lead_rejections r
    JOIN ingest_files f ON f.file_id = r.file_id
    ORDER BY f.workflow_id, f.batch_id, r.row_number, r.file_id"""

Q_INGEST_TOTALS = """
    SELECT
      COALESCE(SUM(CASE WHEN kind = 'leads' THEN rows_total END), 0),
      COALESCE(SUM(CASE WHEN kind = 'leads' THEN rows_rejected END), 0),
      COALESCE(SUM(CASE WHEN kind = 'leads' THEN rows_merged END), 0),
      COALESCE(SUM(CASE WHEN kind = 'leads' THEN field_warnings END), 0),
      COALESCE(SUM(CASE WHEN kind = 'events' THEN lines_total END), 0),
      COALESCE(SUM(CASE WHEN kind = 'events' THEN malformed END), 0),
      COALESCE(SUM(CASE WHEN kind = 'events' THEN duplicates END), 0)
    FROM ingest_files"""

Q_REASONS = "SELECT reason, COUNT(*) FROM lead_rejections GROUP BY reason"

Q_ORPHANS = ("SELECT COUNT(*) FROM events e "
             "WHERE NOT EXISTS (SELECT 1 FROM r_linked l WHERE l.seq = e.seq)")

Q_UNKNOWN = "SELECT COUNT(*) FROM r_unknown_disp"
