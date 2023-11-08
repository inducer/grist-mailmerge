# Basic Email Merge for Grist

Supply a YAML file as configuration, as in [this example](example.yml).
Via the document ID and SQL query, point at data in a
[Grist](https://github.com/gristlabs/grist-core) document. Each row in that
document must have `Email` and `Full_name` columns.

Optionally, it can leave timestamps of when email was sent in that table.

Documentation: See the [this example](example.yml) and the command line help.
