import datetime as dt
import sqlite3

import pytest

import download_txo


HEADER = "交易日期,契約,到期月份(週別),履約價,買賣權,開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,交易時段,契約到期日"


def csv_bytes(row="2026/11/02,TXO,202611W1,40600,買權,10,12,9,11,4,11,7,10.5,11,一般,20261104"):
    return f"{HEADER}\n{row}\n".encode("cp950")


@pytest.mark.parametrize("column,value", [
    (9, "1.5"), (11, "2.25"), (9, "inf"), (11, "nan"),
    (9, "1.000000000000000001"), (11, "9223372036854775808"),
])
def test_integer_fields_reject_fractional_and_nonfinite_values(column, value):
    fields = "2026/11/02,TXO,202611W1,40600,買權,10,12,9,11,4,11,7,10.5,11,一般,20261104".split(",")
    fields[column] = value
    with pytest.raises(ValueError):
        download_txo.parse_csv(csv_bytes(",".join(fields)))


def test_normalized_header_still_reads_trade_date():
    data = (" " + HEADER.replace(",", ", ") + "\n2026/11/02,TXO,202611W1,40600,買權,10,12,9,11,4,11,7,10.5,11,一般,20261104\n").encode("cp950")
    rows = download_txo.parse_csv(data)
    assert rows[0]["trade_date"] == "2026-11-02"


def test_duplicate_raw_primary_key_is_rejected_with_source_row():
    row = "2026/11/02,TXO,202611W1,40600,買權,10,12,9,11,4,11,7,10.5,11,一般,20261104"
    data = f"{HEADER}\n{row}\n{row}\n".encode("cp950")

    with pytest.raises(ValueError, match="重複主鍵") as error:
        download_txo.parse_csv(data)

    assert "2026/11/02" in str(error.value)


def test_integer_fields_preserve_values_beyond_float_exactness():
    fields = "2026/11/02,TXO,202611W1,40600,買權,10,12,9,11,9007199254740993,11,7,10.5,11,一般,20261104"
    assert download_txo.parse_csv(csv_bytes(fields))[0]["volume"] == 9007199254740993


def test_different_content_at_same_raw_name_rejects_before_database_write(tmp_path, monkeypatch):
    start = dt.date(2026, 11, 2)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw_path = raw_dir / "20261102_20261102.csv"
    old_raw = csv_bytes()
    raw_path.write_bytes(old_raw)

    class FakeTaifex:
        def csv(self, *_args):
            return csv_bytes("2026/11/02,TXO,202611W1,40600,買權,10,12,9,11,5,11,7,10.5,11,一般,20261104")

    monkeypatch.setattr(download_txo, "Taifex", FakeTaifex)
    db_path = tmp_path / "db.sqlite3"
    with pytest.raises(ValueError, match="內容不同"):
        download_txo.download(start, start, db_path, raw_dir)

    assert raw_path.read_bytes() == old_raw
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM txo_daily").fetchone()[0] == 0


def test_partial_raw_write_is_removed_and_retry_succeeds(tmp_path, monkeypatch):
    start = dt.date(2026, 11, 2)

    class FakeTaifex:
        def csv(self, *_args):
            return csv_bytes()

    class PartialWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            self.stream.write(data[:10])
            raise OSError("simulated partial write")

        def __getattr__(self, name):
            return getattr(self.stream, name)

    monkeypatch.setattr(download_txo, "Taifex", FakeTaifex)
    db_path, raw_dir = tmp_path / "db.sqlite3", tmp_path / "raw"
    real_fdopen = download_txo.os.fdopen
    with monkeypatch.context() as patch:
        patch.setattr(download_txo.os, "fdopen", lambda fd, mode: PartialWriter(real_fdopen(fd, mode)))
        with pytest.raises(OSError, match="partial write"):
            download_txo.download(start, start, db_path, raw_dir)

    raw_path = raw_dir / "20261102_20261102.csv"
    assert not raw_path.exists()
    assert not list(raw_dir.glob("*.tmp"))
    assert download_txo.download(start, start, db_path, raw_dir) == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM txo_daily").fetchone()[0] == 1


def test_download_rejects_rows_outside_requested_date_range(tmp_path, monkeypatch):
    requested = dt.date(2026, 11, 2)

    class FakeTaifex:
        def csv(self, *_args):
            return csv_bytes("2026/11/03,TXO,202611W1,40600,買權,10,12,9,11,4,11,7,10.5,11,一般,20261104")

    monkeypatch.setattr(download_txo, "Taifex", FakeTaifex)
    with pytest.raises(ValueError, match="超出要求日期"):
        download_txo.download(requested, requested, tmp_path / "db.sqlite3", tmp_path / "raw")
    with sqlite3.connect(tmp_path / "db.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM txo_daily").fetchone()[0] == 0


def test_empty_csv_rejected_but_header_only_nontrading_response_is_zero_rows(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="空白 CSV"):
        download_txo.parse_csv(b"")
    with pytest.raises(ValueError, match="空白 CSV"):
        download_txo.parse_csv(b"\r\n")
    assert download_txo.parse_csv(HEADER.encode("cp950")) == []

    class FakeTaifex:
        def csv(self, *_args):
            return HEADER.encode("cp950")

    monkeypatch.setattr(download_txo, "Taifex", FakeTaifex)
    count = download_txo.download(dt.date(2026, 11, 1), dt.date(2026, 11, 1), tmp_path / "db.sqlite3", tmp_path / "raw")
    assert count == 0
    with pytest.raises(ValueError, match="包含交易日的區間"):
        download_txo.download(dt.date(2026, 11, 2), dt.date(2026, 11, 2), tmp_path / "db.sqlite3", tmp_path / "raw")
