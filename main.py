from edgar import set_identity, Company
import csv
set_identity('Emma qs2314@columbia.edu')

#Google
company = Company("AAPL")

#Accessing the 10-K filings and financial statements
filings = company.get_filings(form="10-K")
latest_10k = filings[0]
financials = company.get_financials()
income = financials.income_statement()
print(income)

#Record the data into a CSV file
df = income.to_dataframe()
records = df.to_dict(orient="records")

source_info = {
    "accession_number": latest_10k.accession_number,
    "filing_url": latest_10k.url,
    "form_type": "10-K",
    "company": "AAPL",
}

with open("output.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(source_info.keys()) + list(df.columns))
    writer.writeheader()
    for row in records:
        writer.writerow({**source_info, **row})

print(source_info)
