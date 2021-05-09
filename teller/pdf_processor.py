import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pdfplumber
import tabula

from teller.model import Transaction, AccountType

VISA_TRANSACTION_REGEX = (r"^(?P<dates>(?:\w{3} \d{2} ){2})"
                          r"(?P<description>.+)\s"
                          r"(?P<amount>-?\$[\d,]+\.\d{2})")

TEMPLATES_DIRECTORY = 'tabula_templates'


def get_start_year(pdf_file_name):
    return int(re.search(r"(?<=-)\d{4}", pdf_file_name).group(0))


def get_transactions(data_directory):
    result = set()
    for pdf_path in Path(data_directory).rglob('*.pdf'):
        print(pdf_path.name)
        year = get_start_year(pdf_path.name)
        if pdf_path.parts[-2] == 'visa':
            result |= _parse_visa(pdf_path, year)
        elif pdf_path.parts[-2] == 'chequing':
            transactions = _parse_cheq_save(pdf_path, year, AccountType.CHEQUING)
            result |= transactions
        elif pdf_path.parts[-2] == 'savings':
            transactions = _parse_cheq_save(pdf_path, year, AccountType.SAVINGS)
            result |= transactions
    return result


def _get_bounding_box_for_transactions(page):
    rows = page.extract_words()
    x0 = 0
    top = 0
    x1 = page.width
    bottom = page.height

    for row in rows:
        if row['text'] == 'TRANSACTION':
            x0 = row['x0']
        if row['text'] == 'AMOUNT($)':
            x1 = row['x1']
            top = row['bottom']

    return x0, top, x1, bottom


def _get_text_for_balances(page):
    rows = page.extract_words()

    x0 = 0
    top = 0
    x1 = page.width
    bottom = page.height

    for row in rows:
        if row['text'] == 'CALCULATINGYOURBALANCE':
            x0 = row['x0']
            top = row['top']
        if row['text'] == 'NEWBALANCE':
            bottom = row['top']

    balance_box = (x0, top, x1, bottom)
    balance_section = page.crop(bbox=balance_box)
    return balance_section.extract_text(x_tolerance=1)


def _parse_visa(pdf_path, year):
    result = set()
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        # get the box with the balance and the bounding box for the left side;
        first_page = pdf.pages[0]
        text_for_balances = _get_text_for_balances(first_page)
        transaction_box = _get_bounding_box_for_transactions(first_page)

        for page in pdf.pages:
            left_side = page.crop(bbox=transaction_box)
            page_text = left_side.extract_text(x_tolerance=1)
            text += '\n' + page_text

        opening_bal = _get_opening_bal(text_for_balances, AccountType.VISA)
        closing_bal = _get_closing_bal(text_for_balances, AccountType.VISA)
        last_month = None
        add_seconds = 0
        for match in re.finditer(VISA_TRANSACTION_REGEX, text, re.MULTILINE):
            match_dict = match.groupdict()
            date = ' '.join(match_dict['dates'].split(' ')[0:2])
            month = datetime.strptime(date.split(' ')[0], '%b').month
            if last_month is not None:
                if month == 1 and last_month == 12:
                    year += 1
                if month == 12 and last_month == 1:
                    year -= 1
            last_month = month
            date = datetime.strptime(date + ' ' + str(year), '%b %d %Y')
            amount = -float(match_dict['amount'].replace('$', '').replace(',', ''))

            transaction = Transaction(AccountType.VISA,
                                      date,
                                      match_dict['description'],
                                      amount)

            transaction.date += timedelta(seconds=add_seconds)
            add_seconds += 1

            result.add(transaction)

    _validate(closing_bal, opening_bal, result)

    return result


def _parse_cheq_save(pdf_path, year, account_type):
    result = set()
    with pdfplumber.open(pdf_path) as pdf:
        template_path = f"{TEMPLATES_DIRECTORY}/{len(pdf.pages)}.json"
        text = ""
        for page in pdf.pages:
            text += page.extract_text(x_tolerance=1)
        opening_bal = _get_opening_bal(text, account_type)
        closing_bal = _get_closing_bal(text, account_type)
    dataframes = tabula.read_pdf_with_template(pdf_path, template_path)
    records = []
    for df in dataframes:
        records.extend(df.where(pd.notnull(df), None).to_dict('records'))
    last_date = None
    add_seconds = 0
    records = records[1:len(records)-1]  # skip Opening/Closing
    for record in records:
        if 'Date Description' in record:
            parts = record['Date Description'].split(' ')
            try:
                if len(parts) > 2 and 0 <= int(parts[0]) <= 31:
                    record['Date'] = ' '.join(parts[:2])
                    record['Description'] = ' '.join(parts[2:])
                else:
                    record['Date'] = None
                    record['Description'] = record['Date Description']
            except ValueError:
                record['Date'] = None
                record['Description'] = record['Date Description']

        if 'Date' not in record:
            continue
        date_str = record['Date']
        if date_str is None:
            date = last_date
        else:
            month = datetime.strptime(date_str.split(' ')[1], '%b').month
            if last_date is not None and month < last_date.month:
                year += 1
            date = datetime.strptime(f"{date_str} {year}", '%d %b %Y')
            last_date = date

        if record['Withdrawals ($)'] is not None:
            amount = -float(str(record['Withdrawals ($)']).replace(',', ''))
        elif record['Deposits ($)'] is not None:
            amount = float(str(record['Deposits ($)']).replace(',', ''))
        else:
            continue
        description = record['Description']

        transaction = Transaction(account_type,
                                  date,
                                  description,
                                  amount)

        transaction.date += timedelta(seconds=add_seconds)
        add_seconds += 1

        result.add(transaction)

    _validate(opening_bal, closing_bal, result)

    return result


def _validate(opening_bal, closing_bal, transactions):
    net = round(sum([r.amount for r in transactions]), 2)
    outflow = round(sum([r.amount for r in transactions if r.amount < 0]), 2)
    inflow = round(sum([r.amount for r in transactions if r.amount > 0]), 2)
    if round(closing_bal - opening_bal, 2) != net:
        print("Note: opening/closing are switched for Visa")
        print(f"Opening reported at {opening_bal}")
        print(f"Closing reported at {closing_bal}")
        print(f"Transactions (net/in/out): {net} / {inflow} / {outflow}")
        print("Parsed transactions:")
        for t in sorted(list(transactions), key=lambda t: t.date):
            print(t)
        raise AssertionError("Discrepancy found, bad parse :(")


def _get_opening_bal(pdf_text, account_type):
    if account_type == AccountType.VISA:
        regex = r'Previous Account Balance (?P<balance>-?\$[\d,]+\.\d{2})'
    else:
        regex = r'Your opening balance.+(?P<balance>-?\$[\d,]+\.\d{2})'
    match = re.search(regex, pdf_text)
    balance = match.groupdict()['balance'].replace(',', '').replace('$', '')
    return float(balance)


def _get_closing_bal(pdf_text, account_type):
    if account_type == AccountType.VISA:
        regex = r'Total Account Balance (?P<balance>-?\$[\d,]+\.\d{2})'
    else:
        regex = r'Your closing balance.+(?P<balance>-?\$[\d,]+\.\d{2})'
    match = re.search(regex, pdf_text)
    balance = match.groupdict()['balance'].replace(',', '').replace('$', '')
    return float(balance)
