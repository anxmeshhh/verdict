"""
Phase 8 data prep - loads the real CVEfixes dataset (Bhandari, Naseer & Moonen,
2021; Zenodo DOI 10.5281/zenodo.4476563) into a local SQLite DB, then exports
the join this benchmark actually needs as a single parquet file.

Why this join: it's the same shape of query Verdict Intelligence's
vulnerability_map needs in production - per service (repo), per vulnerability
class (CWE), how many vulnerable methods, how severe, first/last seen. Running
the identical join+aggregate on CPU vs GPU (rapids_vs_pandas.py) is the actual
acceleration evidence, not a synthetic benchmark invented for the occasion.

Run once (inside the WSL RAPIDS venv - no GPU needed for this step, plain
sqlite3 + pandas):
    python3 prepare_data.py
"""
import gzip
import io
import sqlite3
import zipfile
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
RAW_ZIP = DATA_DIR / "cvefixes_full.zip"
DB_PATH = DATA_DIR / "CVEfixes.db"
PARQUET_PATH = DATA_DIR / "method_vuln_join.parquet"

# The real join: method-level code metrics -> the file they're in -> the commit
# that changed them -> the CVE(s) that commit fixes -> the CWE class(es) of
# that CVE -> the repository (our "service" stand-in). This is exactly the
# shape of verdict Intelligence's vulnerability_map (service x vuln_class x
# severity x first/last-seen), just built from real historical CVE data
# instead of Verdict's own runs.
JOIN_SQL = """
SELECT
    r.repo_name,
    cw.cwe_id,
    cw.cwe_name,
    cve.severity,
    cve.cvss3_base_score,
    c.author_date,
    mc.complexity,
    mc.nloc,
    mc.token_count,
    mc.before_change
FROM method_change mc
JOIN file_change fc   ON mc.file_change_id = fc.file_change_id
JOIN commits c        ON fc.hash = c.hash
JOIN fixes f          ON c.hash = f.hash
JOIN cve                ON f.cve_id = cve.cve_id
JOIN cwe_classification cwc ON cve.cve_id = cwc.cve_id
JOIN cwe cw           ON cwc.cwe_id = cw.cwe_id
JOIN repository r      ON f.repo_url = r.repo_url
"""


def _iter_statements(text_stream):
    """Yield one complete SQL statement at a time from a streaming text
    reader.

    Why this exists: the decompressed dump is several GB of text (the
    compressed .sql.gz alone is ~1GB). Reading it whole with gzip.decompress()
    + str.decode() + conn.executescript() previously loaded the entire dump
    into RAM 2-3x over - on an 8GB laptop that's what took down the whole WSL
    VM (E_UNEXPECTED), not just the Python process. This reads and executes
    one statement at a time instead, so peak memory is bounded by the single
    largest statement, not the whole file.

    This is SQLite's own `.dump` format (confirmed by inspecting the raw
    bytes directly, not assumed): every INSERT is exactly one line - CVEfixes
    escapes embedded newlines/tabs within stored source code as literal text
    specifically so each row stays on one line - and the only multi-line
    statements are the handful of CREATE TABLE ones. SQLite string literals
    escape a quote by doubling it ('') and have no other special character,
    so a statement is complete exactly when its accumulated text (a) ends
    with ';' and (b) contains an EVEN total count of "'" characters - doubled
    escape-quotes always add two at a time, so simple parity of the total
    count is mathematically equivalent to properly tracking string state,
    without needing to scan character-by-character at all. An earlier version
    of this parser guessed at backslash-escaping (mysqldump-style) that this
    format doesn't actually use, and silently fragmented huge amounts of real
    source code into garbage pseudo-statements - this version is verified
    against the actual dump bytes instead of assumed.
    """
    buf = []
    quote_count = 0
    for line in text_stream:
        buf.append(line)
        quote_count += line.count("'")
        if quote_count % 2 == 0 and line.rstrip().endswith(";"):
            stmt = "".join(buf).strip()
            buf = []
            quote_count = 0
            if stmt:
                yield stmt
    tail = "".join(buf).strip()
    if tail:
        yield tail


def build_sqlite_db() -> None:
    if DB_PATH.exists():
        print(f"{DB_PATH} already exists, skipping rebuild")
        return
    print(f"streaming CVEfixes.sql.gz from {RAW_ZIP} (statement-by-statement, not loaded whole into RAM) ...")
    conn = sqlite3.connect(DB_PATH)
    executed = 0
    try:
        with zipfile.ZipFile(RAW_ZIP) as z:
            sql_gz_names = [n for n in z.namelist() if n.endswith(".sql.gz")]
            if not sql_gz_names:
                raise SystemExit(f"no .sql.gz found in {RAW_ZIP} - contents: {z.namelist()[:20]}")
            with z.open(sql_gz_names[0]) as raw, \
                 gzip.GzipFile(fileobj=raw) as gz, \
                 io.TextIOWrapper(gz, encoding="utf-8", errors="replace") as text_stream:
                for stmt in _iter_statements(text_stream):
                    try:
                        conn.execute(stmt)
                    except sqlite3.Error as e:
                        # A handful of malformed/edge statements in a 1GB dump
                        # must not abort the whole load - log and move on.
                        print(f"  [skip] {e} :: {stmt[:120]!r}")
                        continue
                    executed += 1
                    if executed % 5000 == 0:
                        conn.commit()
                        print(f"  {executed:,} statements executed...")
        conn.commit()
    finally:
        conn.close()
    print(f"sqlite DB built - {executed:,} statements executed.")


def export_join() -> None:
    if PARQUET_PATH.exists():
        print(f"{PARQUET_PATH} already exists, skipping")
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(JOIN_SQL, conn)
    finally:
        conn.close()
    print(f"joined table: {len(df):,} rows, {len(df.columns)} columns")
    df.to_parquet(PARQUET_PATH, index=False)
    print(f"wrote {PARQUET_PATH}")


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not RAW_ZIP.exists():
        raise SystemExit(
            f"{RAW_ZIP} not found - download CVEfixes_v1.0.0.zip "
            "(Zenodo DOI 10.5281/zenodo.4476563) into hackathon/benchmark/data/ first"
        )
    build_sqlite_db()
    export_join()
