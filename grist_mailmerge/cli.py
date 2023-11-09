import argparse
import re
from email.headerregistry import Address
from email.message import EmailMessage
from os.path import expanduser
from subprocess import PIPE, Popen

from jinja2 import Environment, StrictUndefined
from pygrist_mini import GristClient
from strictyaml import Map, Optional, Seq, Str, load


YAML_SCHEMA = Map({
    "grist_root_url": Str(),
    "grist_doc_id": Str(),
    "query": Str(),
    "subject_template": Str(),
    "body_template": Str(),
    Optional("timestamp_table"): Str(),
    Optional("timestamp_column"): Str(),
    Optional("cc"): Seq(Str()),
    })


def main():
    parser = argparse.ArgumentParser(description="Email merge for Grist")
    parser.add_argument("filename", metavar="FILENAME.YML")
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--api-key", metavar="FILENAME",
                        default=expanduser("~/.grist-api-key"))
    parser.add_argument("--sendmail", metavar="PATH", default="/usr/bin/sendmail")
    args = parser.parse_args()

    with open(args.filename, "r") as inf:
        yaml_doc = load(inf.read(), YAML_SCHEMA)

    with open(args.api_key, "r") as inf:
        api_key = inf.read().strip()

    client = GristClient(
            yaml_doc["grist_root_url"].text,
            api_key,
            yaml_doc["grist_doc_id"].text)

    rows = client.sql(yaml_doc["query"].text)

    env = Environment(undefined=StrictUndefined)
    subject_template = env.from_string(yaml_doc["subject_template"].text)
    body_template = env.from_string(yaml_doc["body_template"].text)

    timestamp_updates = []
    from time import time
    for row in rows:
        email = row["Email"]
        subject, _ = re.subn(
            r"\s+", " ",subject_template.render(row))
        body = body_template.render(row).strip()
        cc = [yel.text for yel in yaml_doc["cc"]]

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["To"] = Address(row["Full_name"], addr_spec=email)
        msg["Cc"] = tuple([Address(addr_spec=cc) for cc in cc])
        msg.set_content(body)

        if args.dry_run or args.verbose:
            print(75 * "#")
            print(msg)

        if not args.dry_run:
            if "timestamp_column" in yaml_doc:
                timestamp_updates.append((
                    row["id"], {yaml_doc["timestamp_column"].text: time()}))

            with Popen([args.sendmail, "-t", "-i"], stdin=PIPE) as p:
                p.communicate(msg.as_bytes())

    if timestamp_updates:
        client.patch_records(
                yaml_doc["timestamp_table"].text,
                timestamp_updates)
