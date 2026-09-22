#!/usr/bin/env python3
"""Parsing tests. No browser needed: run `python test_check.py`.

These cover the cases that decide whether you hear about the garage or not.
"""

import check

# Shaped like the real page. Note the trap baked in: the heading says "udlejes"
# (a word meaning 'is for rent') while the badge says "udlejet" ('rented out').
PAGE = """LEJEBOLIG
ERHVERVSLEJEMÅL
SPÆNDENDE NYT
OM HEA
P-PLADS / URANIENBORG ALLÉ 1, SØBORG
Garage udlejes på Uranienborg Allé i Søborg
udlejet
Uranienborg Allé 1, 2860 Søborg
650 kr./md.
7.800 kr./år
Lukket garage til opbevaring af bil, motorcykel eller cykel.
Adgang døgnet rundt med personlig lås.
Depositum: Efter aftale
Opsigelse: 3 måneder
Andre lejemål
P-plads i Rødovre 300 kr./md.
Garage i Lyngby 1.100 kr./md.
HEA A/S · Tlf +45 33 31 45 00
"""

FAILURES = []


def check_that(label, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def statuses(page):
    return check.find_statuses(check.extract_listing(page))


def main():
    base = check.extract_listing(PAGE)

    print("\nExtraction")
    check_that("drops the 'Andre lejemål' cross-sell",
               not any("Rødovre" in l for l in base))
    check_that("keeps the listing itself",
               any("Uranienborg" in l for l in base))
    check_that("drops HEA's site nav above the breadcrumb",
               not any("SPÆNDENDE NYT" in l for l in base))
    check_that("a nav rename does not change the fingerprint",
               check.fingerprint(check.extract_listing(
                   PAGE.replace("SPÆNDENDE NYT", "NYHEDER OG TILBUD")))
               == check.fingerprint(base))
    check_that("survives the cross-sell marker being renamed",
               len(check.extract_listing(PAGE.replace("Andre lejemål", "Se også"))) > len(base))

    print("\nStatus reading")
    check_that("the 'udlejes' heading does not mask the 'udlejet' badge",
               check.is_taken(statuses(PAGE)) is True)
    check_that("badge removed -> not taken",
               check.is_taken(statuses(PAGE.replace("udlejet\n", ""))) is False)
    check_that("badge reworded to something unforeseen -> not taken",
               check.is_taken(statuses(PAGE.replace("udlejet", "kan overtages straks"))) is False)
    check_that("badge flipped to 'ledig' -> not taken",
               check.is_taken(statuses(PAGE.replace("udlejet", "ledig"))) is False)
    check_that("a price edit is still taken",
               check.is_taken(statuses(PAGE.replace("650", "700"))) is True)

    print("\nChange detection")
    check_that("a price edit changes the fingerprint",
               check.fingerprint(check.extract_listing(PAGE.replace("650", "700")))
               != check.fingerprint(base))
    check_that("an edit to another listing does NOT change the fingerprint",
               check.fingerprint(check.extract_listing(PAGE.replace("300 kr", "350 kr")))
               == check.fingerprint(base))
    check_that("a dateline does not change the fingerprint",
               check.fingerprint(check.extract_listing(
                   PAGE.replace("Opsigelse", "Opdateret 21/09/2026\nOpsigelse")))
               == check.fingerprint(base))
    check_that("the diff names the status change",
               "ledig" in check.render_diff(
                   base, check.extract_listing(PAGE.replace("udlejet", "ledig"))))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + "; ".join(FAILURES))
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
