"""Invoice facts by code (aish/invoicefacts.py, docs/payments.md "Invoice facts").

Each extractor is tested against its rival: the thing that looks like it and
is not one — a checksum-failing account, a phone number, a NIP, a REGON, a
date and a postal code beside the amounts.
"""

from __future__ import annotations

import pytest

from aish import invoicefacts, vocab

ACCOUNT = "PL72124069601701080000242857"
ACCOUNT_SPACED = "72 1240 6960 1701 0800 0024 2857"
# The same digits with the last one changed: the mod-97 checksum fails.
ACCOUNT_BAD = "72 1240 6960 1701 0800 0024 2858"
IBAN_DE = "DE89370400440532013000"


def accounts(text: str) -> list[str]:
    return [a["account"] for a in invoicefacts.extract(text)["accounts"]]


def amounts(text: str) -> list[tuple[int, str | None]]:
    return [(a["minor"], a["currency"]) for a in invoicefacts.extract(text)["amounts"]]


def amount(text: str, minor: int) -> dict:
    (found,) = [a for a in invoicefacts.extract(text)["amounts"] if a["minor"] == minor]
    return found


class TestInvoiceAccounts:
    @pytest.mark.parametrize("printed", [
        ACCOUNT_SPACED,
        "72124069601701080000242857",
        "PL72124069601701080000242857",
        "PL 72 1240 6960 1701 0800 0024 2857",
        "PL72 1240 6960 1701 0800 0024 2857",
        "7212 4069 6017 0108 0000 2428 57",
        "72-1240-6960-1701-0800-0024-2857",
        "72 1240 6960 1701 0800 0024 2857",
    ])
    def test_every_printed_form_normalises_to_the_one_iban(self, printed):
        assert accounts(f"Nr rachunku: {printed}\n") == [ACCOUNT]

    def test_an_account_split_across_a_line_break(self):
        assert accounts("Numer rachunku: 72 1240 6960 1701\n0800 0024 2857\n") == [ACCOUNT]

    def test_a_checksum_failing_digit_run_is_never_an_account(self):
        assert accounts(f"Nr rachunku: {ACCOUNT_BAD}\n") == []
        assert accounts("PL72124069601701080000242858\n") == []

    def test_a_foreign_iban_and_a_greedy_tail(self):
        assert accounts(f"IBAN: {IBAN_DE}\n") == [IBAN_DE]
        assert accounts("IBAN DE89 3704 0044 0532 0130 00 BANK\n") == [IBAN_DE]

    @pytest.mark.parametrize("text", [
        "NIP 526-104-08-28", "NIP 5261040828", "REGON 012345678",
        "tel. +48 600 700 800", "00-950 Warszawa", "2026-11-10",
        "PESEL 44051401359", "12345678901234567890123456789",
    ])
    def test_identifiers_are_not_accounts(self, text):
        assert accounts(text + "\n") == []

    def test_occurrences_are_counted_and_the_keyword_flag_is_kept(self):
        text = (f"Wpłaty prosimy kierować na rachunek {ACCOUNT_SPACED}\n"
                "Dziękujemy\n\n\n"
                f"stopka {ACCOUNT}\n")
        (found,) = invoicefacts.extract(text)["accounts"]
        assert found == {"account": ACCOUNT, "count": 2, "near_keyword": "line"}
        (alone,) = invoicefacts.extract(f"stopka {ACCOUNT}\n")["accounts"]
        assert alone["near_keyword"] is None
        (next_to,) = invoicefacts.extract(f"Numer rachunku:\n{ACCOUNT}\n")["accounts"]
        assert next_to["near_keyword"] == "adjacent"


class TestInvoiceAmounts:
    @pytest.mark.parametrize("text, minor, currency", [
        ("1 234,56 zł", 123456, "PLN"),
        ("1234,56 zł", 123456, "PLN"),
        ("1.234,56 zł", 123456, "PLN"),
        ("496.33 PLN", 49633, "PLN"),
        ("PLN 496,33", 49633, "PLN"),
        ("1 234,56 zł", 123456, "PLN"),
        ("1 234,56 zł", 123456, "PLN"),
        ("12 345 678,90 zł", 1234567890, "PLN"),
        ("1,234.56 EUR", 123456, "EUR"),
        ("2 789 zł", 278900, "PLN"),
        ("80 zl", 8000, "PLN"),
        ("-12,50 zł", -1250, "PLN"),
        ("−12,50 zł", -1250, "PLN"),
    ])
    def test_every_notation_is_integer_minor_units(self, text, minor, currency):
        # The first reading is the whole number; a plain-space grouping adds
        # its shorter reading after it (TestInvoiceAmountGrouping).
        assert amounts(f"Kwota {text}\n")[0] == (minor, currency)

    @pytest.mark.parametrize("text", [
        "tel. +48 600 700 800", "NIP 526-104-08-28", "NIP 5261040828",
        "REGON 012345678", "00-950 Warszawa", "10.11.2026", "2026-11-10",
        "10-11-2026", "VAT 23%", "VAT 23,00%", "12,345", "rok 2026", ACCOUNT_SPACED,
    ])
    def test_identifiers_dates_and_percentages_are_not_amounts(self, text):
        assert amounts(text + "\n") == []

    def test_a_bullet_before_a_number_is_not_a_minus_sign(self):
        assert amounts("- 496,33 zł\n") == [(49633, "PLN")]
        assert amounts("2026 — 496,33 zł\n") == [(49633, "PLN")]

    def test_no_float_anywhere(self):
        for found in invoicefacts.extract("Razem 0,10 zł 0,20 zł 0.30 PLN\n")["amounts"]:
            assert isinstance(found["minor"], int)
        assert amounts("0,10 zł 0,20 zł\n") == [(10, "PLN"), (20, "PLN")]

    def test_the_document_currency_fills_unmarked_amounts_only_when_it_is_one(self):
        rec = amount("Netto 403,52\nRazem 496,33 zł\n", 40352)
        assert (rec["currency"], rec["currency_source"]) == ("PLN", "document")
        assert amount("Netto 403,52\n", 40352)["currency"] is None
        mixed = amount("Netto 403,52\n10,00 EUR\n5,00 zł\n", 40352)
        assert mixed["currency"] is None

    def test_duplicates_are_counted_once(self):
        found = invoicefacts.extract("496,33 zł\nRazem 496,33 zł\n")["amounts"]
        assert len(found) == 1 and found[0]["count"] == 2

    def test_payable_flag_on_the_line_and_next_to_it(self):
        text = ("Netto 403,52 zł\n"
                "Razem do zapłaty: 496,33 zł\n"
                "Do zaplaty\n"
                "1 000,00 zł\n"
                "\n\n"
                "Opłata 7,00 zł\n")
        assert amount(text, 49633)["payable"] == "line"
        assert amount(text, 100000)["payable"] == "adjacent"
        assert amount(text, 40352)["payable"] == "adjacent"  # the line above a payable one
        assert amount(text, 700)["payable"] is None

    @pytest.mark.parametrize("keyword", [
        "Należność", "NALEZNOSC", "Saldo", "Total", "Amount due", "Kwota do zapłaty",
    ])
    def test_payable_keywords_fold_diacritics_and_case(self, keyword):
        assert amount(f"{keyword}: 12,00 zł\n", 1200)["payable"] == "line"


class TestInvoiceAmountGrouping:
    def test_a_plain_space_grouping_keeps_both_readings_marked(self):
        found = {a["minor"]: a for a in invoicefacts.extract("2 234,56 zł\n")["amounts"]}
        assert set(found) == {223456, 23456}
        assert found[223456]["ambiguous_grouping"] and found[23456]["ambiguous_grouping"]

    @pytest.mark.parametrize("text", ["1 234,56 zł", "1 234,56 zł", "1.234,56 zł",
                                      "1234,56 zł"])
    def test_a_typeset_or_absent_separator_is_not_ambiguous(self, text):
        (found,) = invoicefacts.extract(text + "\n")["amounts"]
        assert found["minor"] == 123456 and found["ambiguous_grouping"] is False

    def test_an_unambiguous_occurrence_elsewhere_clears_the_mark(self):
        found = amount("2 234,56 zł\nRazem 2234,56 zł\n", 223456)
        assert found["ambiguous_grouping"] is False and found["count"] == 2

    def test_currency_seen_tells_a_written_currency_from_a_defaulted_one(self):
        text = "Data 10.11\nRazem 496,33 zł\n"
        assert amount(text, 1011)["currency_seen"] is False
        assert amount(text, 1011)["currency_source"] == "document"
        assert amount(text, 49633)["currency_seen"] is True

    def test_pages_merge_into_one_document(self):
        one = invoicefacts.page_facts("Netto 403,52\n")
        two = invoicefacts.page_facts("Razem do zapłaty 496,33 zł\n")
        assert one["amounts"][0]["currency"] is None  # unresolved on its own page
        merged = invoicefacts.merge([one, two])
        assert amount_in(merged, 40352)["currency"] == "PLN"
        assert merged["keyword_lines"]["payable"] == 1


def amount_in(facts: dict, minor: int) -> dict:
    (found,) = [a for a in facts["amounts"] if a["minor"] == minor]
    return found


class TestInvoiceDates:
    def test_formats(self):
        text = "Data 2026-11-10\nWystawiono 03.11.2026\nDostawa 04-11-2026\n15 listopada 2026\n"
        found = {d["date"] for d in invoicefacts.extract(text)["dates"]}
        assert found == {"2026-11-10", "2026-11-03", "2026-11-04", "2026-11-15"}

    def test_impossible_dates_are_not_dates(self):
        assert invoicefacts.extract("31.02.2026 2026-13-01\n")["dates"] == []

    @pytest.mark.parametrize("line", [
        "Termin płatności: 24.11.2026", "Płatne do 2026-11-24", "zapłać do 24 listopada 2026",
        "TERMIN PLATNOSCI 24-11-2026",
    ])
    def test_due_flag(self, line):
        (found,) = invoicefacts.extract(line + "\n")["dates"]
        assert found == {"date": "2026-11-24", "count": 1, "due": "line"}

    def test_a_date_on_no_due_line_is_kept_unflagged(self):
        (found,) = invoicefacts.extract("Data wystawienia: 10.11.2026\n")["dates"]
        assert found["due"] is None


class TestInvoiceNumbers:
    def numbers(self, text: str) -> list[str]:
        return [i["value"] for i in invoicefacts.extract(text)["invoice_numbers"]]

    def test_the_whole_value_with_its_case(self):
        assert self.numbers("Faktura VAT nr FV/123/2026\n") == ["FV/123/2026"]
        assert self.numbers("Polisa nr: 1023456789\n") == ["1023456789"]

    def test_the_whole_value_is_never_cut_to_its_first_token(self):
        assert self.numbers("Faktura nr FV 123/2026\n") == ["FV 123/2026"]

    def test_the_value_ends_at_a_cell_or_a_wide_gap_and_is_capped(self):
        assert self.numbers("| Numer faktury | 245750201495 |\n") == []
        assert self.numbers("Numer faktury: 2457   Data: 2026\n") == ["2457"]
        long = self.numbers("Nr dokumentu " + "A1" * 40 + "\n")[0]
        assert len(long) == invoicefacts.INVOICE_VALUE_CAP + 1 and long.endswith("…")

    def test_the_next_line_when_the_keyword_ends_its_own(self):
        found = invoicefacts.extract("Numer faktury:\n  245750201495\n")["invoice_numbers"]
        assert found == [{"value": "245750201495", "where": "next line"}]

    def test_the_value_is_cut_to_its_code_run_so_no_words_or_names_are_kept(self):
        assert self.numbers("Faktura VAT nr FV/1/2026 z dnia 10.11.2026\n") == ["FV/1/2026"]
        assert self.numbers("Polisa nr Jan Kowalski 1023456789 ważna\n") == ["1023456789"]
        assert self.numbers("Faktura nr KOR FV 12/2026 do faktury\n") == ["KOR FV 12/2026"]
        assert self.numbers("Numer faktury: 2457 5020 1495\n") == ["2457 5020 1495"]

    def test_a_document_number_must_be_short_and_lead_with_a_digit(self):
        assert self.numbers("Nr dokumentu: 12/2026\n") == ["12/2026"]
        assert self.numbers("Nr dokumentu: Anna Nowak ul. Prosta 5\n") == []
        assert self.numbers("Numer dokumentu tożsamości ABC 123456 wydany\n") == []
        assert self.numbers("Nr dokumentu 1 2 3 4\n") == []
        # An identity-card / PESEL / NIP-shaped bare digit run is no code.
        assert self.numbers("Nr dokumentu: 44051401359\n") == []
        assert self.numbers("Numer dokumentu 123456789\n") == []
        assert self.numbers("Nr dokumentu: 5261040828\n") == []
        assert self.numbers("Nr dokumentu: ABC123456\n") == ["ABC123456"]
        # A strong keyword is not held to that rule.
        assert self.numbers("Numer faktury: 44051401359\n") == ["44051401359"]
        assert self.numbers("Numer faktury: Anna 12/2026\n") == ["12/2026"]

    def test_a_value_without_a_digit_is_no_candidate_and_nothing_is_guessed(self):
        assert self.numbers("Faktura nr do zapłaty\n") == []
        assert self.numbers("FV/123/2026 wystawiona\n") == []


class TestInvoiceTextIsNotKept:
    def test_only_the_named_facts_come_back(self):
        facts = invoicefacts.extract("ACME Sp. z o.o.\nul. Prosta 1\nRazem 1,00 zł\n")
        assert set(facts) == {"accounts", "amounts", "dates", "invoice_numbers", "keyword_lines"}
        assert "ACME" not in repr(facts) and "Prosta" not in repr(facts)

    def test_document_structure_is_not_read_as_the_invoices_numbers(self):
        text = ("[page 1 of 2]\n\n--- OCR text, page 1 ---\nRazem 5,00 zł\n"
                "--- end of OCR text, page 1 ---\n")
        assert amounts(text) == [(500, "PLN")]


class TestInvoiceVocabularies:
    def test_each_list_is_declared_and_counted_per_document(self):
        vocab.reset()
        invoicefacts.extract("Razem do zapłaty 1,00 zł\n")
        tallies = vocab.drain()
        for name in ("invoicefacts._PAYABLE_WORDS", "invoicefacts._DUE_WORDS",
                     "invoicefacts._INVOICE_WORDS", "invoicefacts._ACCOUNT_WORDS",
                     "invoicefacts._MONTHS"):
            assert name in vocab.CATALOGUE and tallies[name]["asked"] == 1
        assert tallies["invoicefacts._PAYABLE_WORDS"]["matched"] == 1
        assert tallies["invoicefacts._DUE_WORDS"]["matched"] == 0
