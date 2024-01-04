import argparse
import ast
import re
from email.headerregistry import Address
from email.message import EmailMessage
from functools import partial
import os
import sys
from subprocess import PIPE, Popen
from typing import Any, Optional as TOptional

from jinja2 import Environment, StrictUndefined
from pygrist_mini import GristClient
from strictyaml import Map, MapPattern, Optional, Seq, Str, load


_EMAIL_ADDR = Map({
    Optional("name"): Str(),
    "email": Str(),
})

YAML_SCHEMA = Map({
    "grist_root_url": Str(),
    "grist_doc_id": Str(),
    "query": Str(),
    "subject": Str(),
    "body": Str(),
    Optional("update"): Map({
        "table": Str(),
        "fields": MapPattern(Str(), Str()),
    }),
    Optional("to"): Seq(_EMAIL_ADDR),
    Optional("cc"): Seq(_EMAIL_ADDR),
    })


def convert_email(expand, email_yml):
    email = expand(email_yml["email"].text)
    if email_yml["name"] is None:
        return Address(addr_spec=email)
    else:
        return Address(
            expand(email_yml["name"].text),
            addr_spec=email)


def convert_emails(expand, emails_yml):
    return tuple(
        convert_email(expand, email_yml)
        for email_yml in emails_yml)


# based on https://stackoverflow.com/a/76636602
def exec_with_return(
        code: str, location: str, globals: TOptional[dict],
        locals: TOptional[dict] = None,
        ) -> Any:
    a = ast.parse(code)
    last_expression = None
    if a.body:
        if isinstance(a_last := a.body[-1], ast.Expr):
            last_expression = ast.unparse(a.body.pop())
        elif isinstance(a_last, ast.Assign):
            last_expression = ast.unparse(a_last.targets[0])
        elif isinstance(a_last, (ast.AnnAssign, ast.AugAssign)):
            last_expression = ast.unparse(a_last.target)
    code = compile(ast.unparse(a), location, "exec")
    exec(code, globals, locals)
    if last_expression:
        return eval(last_expression, globals, locals)


def format_timestamp(tstamp: float, format: str = "%c") -> str:
    import datetime
    dt = datetime.datetime.fromtimestamp(tstamp)
    return dt.strftime(format)


def main():
    parser = argparse.ArgumentParser(description="Email merge for Grist")
    parser.add_argument("filename", metavar="FILENAME.YML")
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--api-key", metavar="FILENAME",
                        default=os.path.expanduser("~/.grist-api-key"))
    parser.add_argument("--sendmail", metavar="PATH", default="/usr/bin/sendmail")
    args = parser.parse_args()

    sys.path.append(os.path.dirname(os.path.abspath(args.filename)))

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
    env.filters["format_timestamp"] = format_timestamp

    def expand_template(context, tpl):
        return env.from_string(tpl).render(context)

    subject_template = env.from_string(yaml_doc["subject"].text)
    body_template = env.from_string(yaml_doc["body"].text)

    updates = []
    for row in rows:
        # {{{ compute row updates

        row_updates = {}
        if yaml_doc["update"] is not None:
            for field, code in yaml_doc["update"]["fields"].data.items():
                globals = dict(row)
                row_updates[field] = exec_with_return(
                    code, f"<update for '{field}'>", globals=globals)
        if row_updates:
            updates.append((row["id"], row_updates))

        if args.verbose or args.dry_run:
            print(row_updates)

        # }}}

        exp_context = row.copy()
        for field, val in row_updates.items():
            exp_context[f"updated_{field}"] = val

        subject, _ = re.subn(
            r"\s+", " ", subject_template.render(exp_context))
        body = body_template.render(exp_context).strip()

        expand = partial(expand_template, exp_context)

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["To"] = convert_emails(expand, yaml_doc["to"])
        msg["Cc"] = convert_emails(expand, yaml_doc["cc"])
        msg.set_content(body)

        if args.dry_run or args.verbose:
            print(75 * "#")
            print(msg)

        if not args.dry_run:
            with Popen([args.sendmail, "-t", "-i"], stdin=PIPE) as p:
                p.communicate(msg.as_bytes())

    if yaml_doc["update"] is not None and not args.dry_run:
        client.patch_records(
                yaml_doc["update"]["table"].text,
                updates)

# vim: foldmethod=marker
