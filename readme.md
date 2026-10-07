# Shared SEC Filing Database

This project stores information about company reports filed with the U.S. Securities and Exchange Commission (SEC). The database is online, so approved teammates can view the same information from their own computers.

**This guide shows you how to open the database and view its contents without writing code.** You will use DBeaver, a free desktop app that displays the information in rows and columns, much like a spreadsheet. You do not need an AWS account.

## 1. Ask the project administrator for access

Michael is the project administrator and manages access to the database.

Michael has already created individual read-only accounts for Jace, Bryce, Jazzy, Emma, Ruby, and Sally. Request your own credentials privately. Access from your network still needs approval and a connection test.

Ask Michael for:

- Your own **database username and password**.
- Permission to connect from your current internet connection.
- **Read-only access** if you only need to view information. This lets you browse without changing the shared data.

To arrange the internet connection permission, open [this AWS page](https://checkip.amazonaws.com/), copy the address it shows, and send it to the administrator. This is your **public IP address**—the address your internet connection uses. Tell them if you will be using a VPN.

**Wait for the administrator to confirm access before continuing.** Having this README does not automatically give you access. If you later change Wi-Fi networks or turn a VPN on or off, the administrator may need to approve your new address.

## 2. Install the app and download the security file

1. Download [DBeaver Community](https://dbeaver.io/download/), choose the version for your computer, and install it.
2. Download the [AWS security certificate](https://truststore.pki.rds.amazonaws.com/us-east-1/us-east-1-bundle.pem). This small file helps DBeaver check that it is connecting to the correct server.
3. Save it somewhere you can find again, such as a folder called **Database Access** in Documents. Keep the filename **`us-east-1-bundle.pem`**. Do not delete or move it after connecting.

If the certificate link opens a page of text instead of downloading a file, save that page with the filename above. It should end in `.pem`, not `.html` or `.txt`.

## 3. Tell DBeaver which database to open

Open DBeaver and choose **Database → New Database Connection**. Select **PostgreSQL**, then click **Next**. PostgreSQL is simply the type of database we use.

Check whether DBeaver is asking for a **Host** or a **URL**. They use different formats. In Host mode, fill in Host, Port, and Database separately. In URL mode, use the full URL below instead.

| Box in DBeaver | What to enter |
| --- | --- |
| **Host** (Host mode only) | `sec-filings.cghyw6082mug.us-east-1.rds.amazonaws.com` — enter this only in the **Host** box. |
| **Port** | `5432` |
| **Database** | `sec_filings` |
| **URL** (URL mode only) | `jdbc:postgresql://sec-filings.cghyw6082mug.us-east-1.rds.amazonaws.com:5432/sec_filings` |
| **Username** | The database username the administrator gave you |
| **Password** | The matching database password |

Putting only the Host address into the **URL** box causes **Invalid JDBC URL**. The full URL includes the required `jdbc:postgresql://` beginning, port, and database name. Enter these values in DBeaver, not your web browser. The username and password are for this database, not for an AWS account.

## 4. Set up the secure connection

In the same connection window:

1. Open the **SSL** tab. If you cannot see it, click **+** and select **SSL**.
2. If there is a **Use SSL** checkbox, turn it on.
3. Set **SSL Mode** to **`verify-full`**.
4. In **CA Certificate**, choose the `us-east-1-bundle.pem` file you saved in step 2.
5. Leave **Client Certificate** and **Client Private Key** empty.
6. Click **Test Connection**. If DBeaver asks to download a PostgreSQL driver, allow it; this is the component it needs to talk to the database.
7. When the test succeeds, click **Finish**.

SSL protects the connection. The `verify-full` setting also checks the server's identity. Keep that setting enabled. If the options on your screen differ, ask the administrator to help with the [SSL settings](https://dbeaver.com/docs/dbeaver/SSL-Configuration/).

**Success at this step:** the connection test succeeds, and your new database connection appears in DBeaver's left-hand panel.

## 5. Open the filing list

You can now browse the data by clicking through the folders:

1. Expand your saved connection in the left-hand panel. It may be named **postgres**, or another name you chose. Open **Databases → sec_filings** if the Databases folder is shown.
2. Open **Schemas → sec → Views**. Think of `sec` as the folder containing this project's information.
3. Double-click **filing_catalog**.
4. Select the **Data** tab to display its rows.

`filing_catalog` is a prepared summary of the reports in the database. You do not need to write a query to view it. See [DBeaver's Data tab help](https://dbeaver.com/docs/dbeaver/Data-Editor/) if needed.

These column names may be unfamiliar:

| Column | What it means |
| --- | --- |
| `company_name` | The company that submitted the report |
| `form` | The type of report; `10-Q` means a quarterly report |
| `filing_date` | The date the report was filed |
| `accession_number` | The report's unique reference number |
| `source_document_count` | How many original source files are recorded for that report |

**What you should see:** the initial sample is one Microsoft quarterly report, reference `0001193125-26-191507`, with **4** in `source_document_count`.

The example loaded on October 6, 2026 contains **24 financial facts, 3 statement excerpts, 1 report excerpt, and 6 text chunks**, labeled `manual-example-v1`. These are selected real values and passages from the Microsoft filing; the team's complete extraction pipeline is still being built. The [example guide](docs/manual-example.md) explains how to read the values, run six ready-to-use queries, and check the expected results.

This example is in the **shared AWS database**. A local database created with the developer setup starts with filing details and four source-file records only. In either database, source-file records contain links and file details; connecting does not download the original documents.

The next time you want to use the database, open DBeaver, connect to the saved connection, and open **filing_catalog → Data** again.

## 6. Explore the individual tables

The filing list is a summary. To see the information behind it:

1. In the left-hand panel, open **your connection → Databases → sec_filings → Schemas → sec → Tables**. If your connection does not show a Databases folder, go directly to **Schemas → sec → Tables**.
2. Double-click a table name, such as **companies**.
3. Click its **Data** tab to see the rows.

Clicking the small arrow beside a table expands details such as its columns. To view the actual records, double-click the table's name and choose **Data**.

| Table | What you will find there |
| --- | --- |
| **companies** | Company names and identification numbers |
| **filings** | The reports each company filed, including dates and reference numbers |
| **source_documents** | Details and source links for the original files belonging to each report |
| **reports** | Processed text extracted from the original documents |
| **chunks** | Smaller sections of that text, used to find and cite specific passages |
| **financial_facts** | Individual financial figures, such as revenue, with their dates, units, and sources |
| **financial_tables** | Extracted financial statement tables |
| **financial_table_facts** | Links showing which financial figures belong to which statement tables |
| **schema_migrations** | The database's technical update history; you can skip this when browsing the data |

One company can have many filings. Each filing can have several source documents. As the team processes those documents, it adds report text, smaller text sections, and financial figures to the related tables.

Columns ending in **`_id`** connect related records. For example, a filing's **company_id** matches the **id** of its company, and a source document's **filing_id** matches the **id** of its filing. These long identifiers let the database keep the relationships clear.

### Try this with the Microsoft sample

1. Open **companies → Data** and find Microsoft.
2. Open **filings → Data** and find report reference `0001193125-26-191507` in the **accession_number** column.
3. Open **source_documents → Data** to see the four original-file records from the imported sample. The **source_url** column contains the original web addresses.
4. Under **sec → Views**, open **fact_provenance → Data** for financial figures and their sources, or **chunk_citations → Data** for text passages and citations.
5. Under **sec → Tables**, open **financial_tables → Data** and read **readable_text** for the statement excerpts. Return to **Views → filing_catalog → Data** for the combined overview.

These tables and views can contain multiple filings and extraction versions. The [six sample queries](docs/manual-example-queries.sql) select the Microsoft filing and `manual-example-v1` for you, so their expected counts remain useful as the team adds more data. Query 1 shows the counts; query 2 shows the 24 facts; query 6 shows the six passages and checks that they match the stored report excerpt.

When reading a financial figure, keep its **unit and period** beside it. For example, a value stored in dollars differs from a statement displayed in millions, and a three-month amount differs from a nine-month amount even when they end on the same date. The [example guide](docs/manual-example.md#read-the-values-correctly) explains both cases. If you see **Permission denied**, ask the administrator for access to the specific table or view.

## 7. Understand the tabs and refresh the data

When you open a table, DBeaver provides different ways to look at it:

| Tab or button | What it does |
| --- | --- |
| **Data** | Shows the actual records in rows and columns. Use this for everyday browsing. |
| **Properties** | Describes the table: its column names, data types, and other settings. |
| **Diagram** | Shows how the table relates to other tables. |
| **Refresh / F5** | Reloads the information, for example after another team has added records. |

If you opened **chunks** and only see a list of column names such as `id`, `filing_id`, and `text`, you are looking at **Properties**. Select **Data** to switch to the records themselves.

## If something does not work

| What you see | What to do |
| --- | --- |
| **Invalid JDBC URL** | Check the field label: **Host** takes the server address alone; **URL** takes the full `jdbc:postgresql://...:5432/sec_filings` value from step 3. |
| The connection keeps waiting or says **timed out** | Send the administrator your current public IP address. Mention any Wi-Fi or VPN change. |
| **Password authentication failed** | Check that you used the database username and password provided to you. Ask the administrator to check your login if it still fails. |
| A **certificate** or **SSL** error | Check that the security file is still in the same folder and selected in the SSL tab. Use the Host address exactly as shown above. |
| **Permission denied** | You connected, but your login is not allowed to open that item. Send its name to the administrator. |
| You cannot find **sec** or **filing_catalog** | Check that you opened `sec_filings`, then **Schemas → sec → Views**. Ask the administrator if it is missing. |
| Some views contain no rows | Press **F5**, clear old filters, and confirm you selected the shared RDS database. The manual example includes 24 facts and 6 chunks; the separate local database may contain only filing metadata. |

When asking for help, include the error message and which step you reached. Keep your password out of messages and screenshots.

## For developers and administrators

These guides are optional if you only want to view the data:

- [GitHub tutorial: personal branches, commits, and pull requests to main](docs/github-tutorial.md)
- [Python access, SQL examples, and project commands](docs/developer-usage.md)
- [Manual data example: six queries and expected results](docs/manual-example.md)
- [Database structure and local setup](docs/database.md)
- [AWS deployment and maintenance](docs/aws-deployment.md)
- [Team implementation plan](docs/team-implementation-plan.md)

This is a QMSS practicum project using public SEC filings. Additional tools for verifying source files and exporting income statements are described in the developer guide.
