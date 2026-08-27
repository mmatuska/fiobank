from __future__ import annotations

import io
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import date
from decimal import Decimal
from unittest import mock

import pycurl
import pytest

from fiobank import FioBank, HTTPError
from fiobank.models import Transaction


BASE_URL = "https://fioapi.fio.cz/v1/rest/"


class _MockCurl:
    """Minimal pycurl.Curl mock that writes a fixed body on perform()."""

    _url_pattern = re.compile(
        r"^https://fioapi\.fio\.cz/v1/rest/"
        r"(periods/[^/]+/\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}/transactions\.json"
        r"|by-id/[^/]+/\d{4}/\d+/transactions\.json"
        r"|last/[^/]+/transactions\.json"
        r"|set-last-id/[^/]+/\d+/"
        r"|set-last-date/[^/]+/\d{4}-\d{2}-\d{2}/"
        r"|lastStatement/[^/]+/statement(\?year=\d{4})?)$"
    )

    def __init__(self, status: int = 200, body: bytes = b""):
        self._status = status
        self._body = body
        self._write_buffer: io.BytesIO | None = None
        self._url: str | None = None
        self._params: dict = {}

    def setopt(self, option, value):
        if option == pycurl.WRITEDATA:
            self._write_buffer = value
        elif option == pycurl.URL:
            self._url = value

    def perform(self):
        if self._url is None or not self._url_pattern.match(self._url):
            raise AssertionError(f"Unexpected request URL: {self._url!r}")
        if self._write_buffer is not None:
            self._write_buffer.write(self._body)

    def getinfo(self, option):
        if option == pycurl.RESPONSE_CODE:
            return self._status
        return None

    def close(self):
        pass


def _make_curl_factory(*responses_seq):
    """Return a factory that yields successive _MockCurl instances per call."""
    it = iter(responses_seq)

    def factory():
        return next(it)

    return factory


@pytest.fixture()
def token() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def transactions_text() -> str:
    with open(os.path.dirname(__file__) + "/transactions.json") as f:
        return f.read()


@pytest.fixture()
def transactions_json() -> dict:
    with open(os.path.dirname(__file__) + "/transactions.json") as f:
        return json.load(f)


@pytest.fixture()
def client_float(token: str, transactions_text: str):
    body = transactions_text.encode()

    # Return mock curl instances for any number of calls: transactions first, empty for set-last-*
    def factory():
        return _MockCurl(200, body)

    with mock.patch("fiobank.fiobank.pycurl.Curl", side_effect=factory):
        yield FioBank(token)


@pytest.fixture()
def client_decimal(token: str, transactions_text: str):
    body = transactions_text.encode()

    def factory():
        return _MockCurl(200, body)

    with mock.patch("fiobank.fiobank.pycurl.Curl", side_effect=factory):
        yield FioBank(token, decimal=True)


def test_client_decimal(token: str, transactions_text: str):
    body = transactions_text.encode()

    def factory():
        return _MockCurl(200, body)

    with mock.patch("fiobank.fiobank.pycurl.Curl", side_effect=factory):
        client = FioBank(token, decimal=True)
        transaction = next(client.last())
        info = client.info()

    assert client.float_type is Decimal
    assert transaction["amount"] == Decimal("-130.0")
    assert info["balance"] == Decimal("2060.52")


def test_info_integration(token: str, transactions_text: str):
    body = transactions_text.encode()

    def factory():
        return _MockCurl(200, body)

    with mock.patch("fiobank.fiobank.pycurl.Curl", side_effect=factory):
        client = FioBank(token)
        assert frozenset(client.info().keys()) == frozenset(
            [
                "account_number_full",
                "account_number",
                "bank_code",
                "currency",
                "iban",
                "bic",
                "balance",
            ]
        )


def test_info_uses_today(transactions_json: dict):
    client = FioBank("...")
    today = date.today()

    with mock.patch.object(
        client, "_request_json", return_value=transactions_json
    ) as stub:
        client.info()
        stub.assert_called_once_with("periods", from_date=today, to_date=today)


@pytest.mark.parametrize(
    "api_key, sdk_key",
    [
        ("accountId", "account_number"),
        ("bankId", "bank_code"),
        ("currency", "currency"),
        ("iban", "iban"),
        ("bic", "bic"),
        ("closingBalance", "balance"),
    ],
)
def test_info_parse(transactions_json, api_key, sdk_key):
    client = FioBank("...")

    api_info = transactions_json["accountStatement"]["info"]
    sdk_info = client._parse_info(transactions_json)

    assert sdk_info[sdk_key] == api_info[api_key]


def test_info_parse_account_number_full(transactions_json):
    client = FioBank("...")

    api_info = transactions_json["accountStatement"]["info"]
    sdk_info = client._parse_info(transactions_json)

    expected_value = "{}/{}".format(api_info["accountId"], api_info["bankId"])
    assert sdk_info["account_number_full"] == expected_value


def test_info_parse_no_account_number_full(transactions_json):
    client = FioBank("...")

    api_info = transactions_json["accountStatement"]["info"]
    del api_info["bankId"]

    sdk_info = client._parse_info(transactions_json)

    assert sdk_info["account_number_full"] is None


@pytest.mark.parametrize(
    "method, args, kwargs",
    [
        ("period", [date(2016, 8, 4), date(2016, 8, 30)], {}),
        ("period", ["2016-08-04", "2016-08-30"], {}),
        ("statement", [2016, 308], {}),
        ("last", [], {"from_id": 308}),
        ("last", [], {"from_date": date(2016, 8, 4)}),
        ("last", [], {"from_date": "2016-08-04"}),
    ],
)
def test_transactions_integration(client_float, method, args, kwargs):
    gen = getattr(client_float, method)(*args, **kwargs)

    count = 0
    for record in gen:
        count += 1
        assert frozenset(record.keys()) == frozenset(
            [
                "transaction_id",
                "date",
                "amount",
                "currency",
                "account_number",
                "account_name",
                "bank_code",
                "bic",
                "bank_name",
                "constant_symbol",
                "variable_symbol",
                "specific_symbol",
                "user_identification",
                "recipient_message",
                "type",
                "executor",
                "specification",
                "comment",
                "instruction_id",
                "account_number_full",
                "original_amount",
                "original_currency",
                "reference",
            ]
        )

    assert count > 0


@pytest.mark.parametrize(
    "args,kwargs",
    [
        ([date(2016, 8, 4), date(2016, 8, 30)], {}),
        (["2016-08-04", "2016-08-30"], {}),
    ],
)
def test_transactions(token: str, transactions_text: str, args, kwargs):
    body = transactions_text.encode()

    def factory():
        return _MockCurl(200, body)

    with mock.patch("fiobank.fiobank.pycurl.Curl", side_effect=factory):
        client = FioBank(token, decimal=True)
        info, transactions = client.transactions(*args, **kwargs)
        transaction = next(transactions)
        assert transaction["amount"] == Decimal("-130.0")
        assert info["balance"] == Decimal("2060.52")


@pytest.mark.parametrize(
    "args,kwargs",
    [
        ([], {"from_id": 308}),
        ([], {"from_date": date(2016, 8, 4)}),
        ([], {"from_date": "2016-08-04"}),
    ],
)
def test_last_transactions(token: str, transactions_text: str, args, kwargs):
    body = transactions_text.encode()

    def factory():
        return _MockCurl(200, body)

    with mock.patch("fiobank.fiobank.pycurl.Curl", side_effect=factory):
        client = FioBank(token, decimal=True)
        info, transactions = client.last_transactions(*args, **kwargs)
        transaction = next(transactions)
        assert transaction["amount"] == Decimal("-130.0")
        assert info["balance"] == Decimal("2060.52")


def test_period_coerces_date(transactions_json):
    client = FioBank("...")

    from_date = "2016-08-04T09:36:42"
    to_date = "2016-08-30T11:45:38"

    options = {"return_value": transactions_json}
    with mock.patch.object(client, "_request_json", **options) as stub:
        client.period(from_date, to_date)
        stub.assert_called_once_with(
            "periods", from_date=date(2016, 8, 4), to_date=date(2016, 8, 30)
        )


def test_statement(transactions_json):
    client = FioBank("...")

    options = {"return_value": transactions_json}
    with mock.patch.object(client, "_request_json", **options) as stub:
        client.statement(2016, 308)
        stub.assert_called_once_with("by-id", year=2016, number=308)


@pytest.mark.parametrize("bad_token", [None, "", "   "])
def test_invalid_token(bad_token):
    with pytest.raises(ValueError, match="Token cannot be None or empty"):
        FioBank(token=bad_token)


def test_default_base_url():
    client = FioBank("...")
    assert client.base_url == BASE_URL


def test_default_request_timeout():
    client = FioBank("...")
    assert client.request_timeout == 60


def test_last_conflicting_params():
    client = FioBank("...")
    with pytest.raises(ValueError):
        client.last(from_id=308, from_date=date(2016, 8, 30))


def test_last_from_id(transactions_json):
    client = FioBank("...")

    options = {"return_value": transactions_json}
    with mock.patch.object(client, "_request_json", **options) as stub:
        client.last(from_id=308)
        stub.assert_has_calls(
            [
                mock.call("set-last-id", from_id=308),
                mock.call("last"),
            ]
        )


@pytest.mark.parametrize("test_input", [date(2016, 8, 30), "2016-08-30"])
def test_last_from_date(transactions_json, test_input):
    client = FioBank("...")

    options = {"return_value": transactions_json}
    with mock.patch.object(client, "_request_json", **options) as stub:
        client.last(from_date=test_input)
        stub.assert_has_calls(
            [
                mock.call("set-last-date", from_date=date(2016, 8, 30)),
                mock.call("last"),
            ]
        )


def test_transaction_schema_is_complete():
    # The XSD lives on www.fio.cz, which might not be reachable from some sandboxed
    # runtimes (e.g. AI agent coding environment). Skip cleanly there
    # rather than failing. CI has network access and runs the assertion.
    try:
        req = urllib.request.urlopen("https://www.fio.cz/xsd/IBSchema.xsd", timeout=10)
        xsd_text = req.read().decode("utf-8")
    except (OSError, urllib.error.URLError) as e:
        pytest.skip(f"www.fio.cz is not reachable from this runtime: {e}")

    columns_in_xsd = set()

    element_re = re.compile(r'<\w+:element[^>]+name="column_(\d+)')
    for match in element_re.finditer(xsd_text):
        column_name = f"column{match.group(1)}"
        columns_in_xsd.add(column_name)

    columns_in_model = {
        field.alias for field in Transaction.model_fields.values() if field.alias
    }
    assert columns_in_model == columns_in_xsd


@pytest.mark.parametrize(
    "api_key, sdk_key",
    [(field.alias, name) for name, field in Transaction.model_fields.items()],
)
def test_transactions_parse(transactions_json, api_key, sdk_key):
    client = FioBank("...")

    api_transactions = transactions_json["accountStatement"]["transactionList"][
        "transaction"
    ]

    # The 'transactions.json' file is based on real data, so it doesn't
    # contain some values. To test all values, we use dummy data here.
    dummy_mapping = {"column0": "2015-08-30", "column1": 30.8}
    dummy_default = "dummy"
    for api_transaction in api_transactions:
        dummy_value = dummy_mapping.get(api_key, dummy_default)
        api_transaction[api_key] = {"value": dummy_value}

    # date and amount are converted, everything else is a plain string
    expected_mapping = {"column0": date(2015, 8, 30), "column1": 30.8}
    expected_value = expected_mapping.get(api_key, "dummy")

    sdk_transactions = list(client._parse_transactions(transactions_json))
    assert len(sdk_transactions) == len(api_transactions)

    for sdk_transaction in sdk_transactions:
        assert sdk_transaction[sdk_key] == expected_value


def test_transactions_parse_unsanitized(transactions_json):
    client = FioBank("...")

    api_transaction = transactions_json["accountStatement"]["transactionList"][
        "transaction"
    ][0]
    api_transaction["column10"] = {"value": "             Honza\n"}

    sdk_transaction = next(client._parse_transactions(transactions_json))

    assert sdk_transaction["account_name"] == "Honza"


def test_transactions_parse_convert(transactions_json):
    client = FioBank("...")

    api_transaction = transactions_json["accountStatement"]["transactionList"][
        "transaction"
    ][0]
    api_transaction["column0"] = {"value": "2015-08-30"}

    sdk_transaction = next(client._parse_transactions(transactions_json))

    assert sdk_transaction["date"] == date(2015, 8, 30)


def test_transactions_parse_none(transactions_json):
    client = FioBank("...")

    api_transaction = transactions_json["accountStatement"]["transactionList"][
        "transaction"
    ][0]
    sdk_transaction = next(client._parse_transactions(transactions_json))

    assert api_transaction["column10"] is None
    assert sdk_transaction["account_name"] is None


def test_transactions_parse_missing(transactions_json):
    client = FioBank("...")

    api_transaction = transactions_json["accountStatement"]["transactionList"][
        "transaction"
    ][0]
    del api_transaction["column10"]

    sdk_transaction = next(client._parse_transactions(transactions_json))

    assert "column10" not in api_transaction
    assert sdk_transaction["account_name"] is None


@pytest.mark.parametrize(
    "test_input",
    [
        "650.00 HRK",
        "-308 EUR",
        "46052.01 HUF",
    ],
)
def test_amount_re(test_input):
    assert FioBank._amount_re.match(test_input)


@pytest.mark.parametrize(
    "test_input, amount, currency",
    [
        ("650.00 HRK", 650.0, "HRK"),
        ("-308 EUR", -308.0, "EUR"),
        ("46052.01 HUF", 46052.01, "HUF"),
    ],
)
def test_transactions_parse_amount_as_float(
    transactions_json, test_input, amount, currency
):
    client = FioBank("...")

    api_transaction = transactions_json["accountStatement"]["transactionList"][
        "transaction"
    ][0]
    api_transaction["column18"] = {"value": test_input}

    sdk_transaction = next(client._parse_transactions(transactions_json))

    assert sdk_transaction["specification"] == test_input
    assert sdk_transaction["original_amount"] == amount
    assert sdk_transaction["original_currency"] == currency


@pytest.mark.parametrize(
    "test_input, amount, currency",
    [
        ("650.00 HRK", Decimal("650.0"), "HRK"),
        ("-308 EUR", Decimal("-308.0"), "EUR"),
        ("46052.01 HUF", Decimal("46052.01"), "HUF"),
    ],
)
def test_transactions_parse_amount_as_decimal(
    transactions_json, test_input, amount, currency
):
    client = FioBank("...", decimal=True)

    api_transaction = transactions_json["accountStatement"]["transactionList"][
        "transaction"
    ][0]
    api_transaction["column18"] = {"value": test_input}

    sdk_transaction = next(client._parse_transactions(transactions_json))

    assert sdk_transaction["specification"] == test_input
    assert sdk_transaction["original_amount"] == amount
    assert sdk_transaction["original_currency"] == currency


def test_transactions_parse_account_number_full(transactions_json):
    client = FioBank("...")

    api_transaction = transactions_json["accountStatement"]["transactionList"][
        "transaction"
    ][0]
    api_transaction["column2"] = {"value": 10000000002}
    api_transaction["column3"] = {"value": "2010"}

    sdk_transaction = next(client._parse_transactions(transactions_json))

    assert sdk_transaction["account_number_full"] == "10000000002/2010"


def test_transactions_parse_no_account_number_full(transactions_json):
    client = FioBank("...")

    api_transaction = transactions_json["accountStatement"]["transactionList"][
        "transaction"
    ][0]
    api_transaction["column2"] = {"value": 10000000002}
    api_transaction["column3"] = {"value": None}

    sdk_transaction = next(client._parse_transactions(transactions_json))

    assert sdk_transaction["account_number_full"] is None


def test_last_statement(token: str, transactions_text: str):
    statement_body = b"2017,12"
    transactions_body = transactions_text.encode()

    # First call: lastStatement endpoint; second call: by-id transactions
    calls = [
        _MockCurl(200, statement_body),
        _MockCurl(200, transactions_body),
    ]
    it = iter(calls)

    with mock.patch("fiobank.fiobank.pycurl.Curl", side_effect=lambda: next(it)):
        client = FioBank(token, decimal=True)
        transactions = list(client.last_statement())

    assert len(transactions) > 0


def test_last_statement_year(token: str, transactions_text: str):
    statement_body = b"2016,3"
    transactions_body = transactions_text.encode()

    recorded_urls: list[str] = []

    class RecordingCurl(_MockCurl):
        def setopt(self, option, value):
            super().setopt(option, value)
            if option == pycurl.URL:
                recorded_urls.append(value)

    rec_calls = [
        RecordingCurl.__new__(RecordingCurl),
        RecordingCurl.__new__(RecordingCurl),
    ]
    rec_calls[0].__init__(200, statement_body)
    rec_calls[1].__init__(200, transactions_body)

    it = iter(rec_calls)
    with mock.patch("fiobank.fiobank.pycurl.Curl", side_effect=lambda: next(it)):
        client = FioBank(token, decimal=True)
        list(client.last_statement(2016))

    assert "year=2016" in recorded_urls[0]
    assert f"by-id/{token}/2016/3/transactions.json" in recorded_urls[1]


def test_last_statement_none(token: str):
    calls = [_MockCurl(200, b"null,null")]
    it = iter(calls)

    with mock.patch("fiobank.fiobank.pycurl.Curl", side_effect=lambda: next(it)):
        client = FioBank(token, decimal=True)
        with pytest.raises(ValueError, match="No data available"):
            client.last_statement(2000)


def test_409_conflict(token: str, transactions_text: str):
    body = transactions_text.encode()
    calls = [
        _MockCurl(409, b""),
        _MockCurl(200, body),
    ]
    it = iter(calls)

    with mock.patch("fiobank.fiobank.pycurl.Curl", side_effect=lambda: next(it)):
        client = FioBank(token, decimal=True)
        transaction = next(client.last())

    assert transaction["amount"] == Decimal("-130.0")


@pytest.mark.parametrize("attr", ["transaction_schema", "info_schema"])
def test_removed_schema_attributes(attr):
    client = FioBank("...")
    with pytest.raises(NotImplementedError):
        getattr(client, attr)


def test_http_error_with_token_redaction(token: str):
    response_body = f"Error occurred with token {token} in the response body"

    calls = [_MockCurl(400, response_body.encode())]
    it = iter(calls)

    with mock.patch("fiobank.fiobank.pycurl.Curl", side_effect=lambda: next(it)):
        client = FioBank(token, decimal=True)
        with pytest.raises(HTTPError) as exc_info:
            list(client.period("2025-01-01", "2025-02-01"))

    error_msg = str(exc_info.value)
    assert token not in error_msg
    assert "***TOKEN***" in error_msg
    assert "Error occurred with token ***TOKEN*** in the response body" in error_msg
