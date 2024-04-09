import argparse
import ast
import os
import re
import sys
from email.headerregistry import Address
from email.message import EmailMessage
from functools import partial
from subprocess import PIPE, Popen
from typing import Any, Dict, List, Optional as TOptional, Tuple

from jinja2 import Environment, StrictUndefined
from pygrist_mini import GristClient
from strictyaml import Bool, Map, MapPattern, Optional, Seq, Str, load


_EMAIL_ADDR = Map({
    Optional("name"): Str(),
    "email": Str(),
    Optional("semicolon_separated"): Bool(),
})

YAML_SCHEMA = Map({
    "grist_root_url": Str(),
    "grist_doc_id": Str(),
    Optional("parameters"): Seq(Str()),
    "query": Str(),
    "subject": Str(),
    "body": Str(),
    Optional("update"): Map({
        "table": Str(),
        "fields": MapPattern(Str(), Str()),
    }),
    Optional("insert"): Seq(
        Map({
            "table": Str(),
            "fields": MapPattern(Str(), Str()),
        }),
    ),
    Optional("to"): Seq(_EMAIL_ADDR),
    Optional("cc"): Seq(_EMAIL_ADDR),
    })


def convert_email(expand, email_yml):
    if email_yml.get("semicolon_separated", False):
        emails = [email.strip()
            for email in expand(email_yml["email"].text).split(";")]
        if "name" not in email_yml:
            return tuple(Address(addr_spec=email) for email in emails if email)
        else:
            names = expand(email_yml["name"].text).split(";")
            return tuple(
                Address(name, addr_spec=email)
                for name, email in zip(names, emails, strict=True) if email)

    else:
        email = expand(email_yml["email"].text)
        if not email:
            return ()

        if "name" not in email_yml:
            return (Address(addr_spec=email),)
        else:
            return (Address(
                expand(email_yml["name"].text),
                addr_spec=email),)


def convert_emails(expand, emails_yml):
    return tuple(
        addr
        for email_yml in emails_yml
        for addr in convert_email(expand, email_yml))


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
    compiled_code = compile(ast.unparse(a), location, "exec")
    exec(compiled_code, globals, locals)
    if last_expression:
        return eval(last_expression, globals, locals)


def format_timestamp(tstamp: float, format: str = "%c") -> str:
    import datetime
    dt = datetime.datetime.fromtimestamp(tstamp)
    return dt.strftime(format)


def main():
    env = Environment(undefined=StrictUndefined)
    env.filters["format_timestamp"] = format_timestamp

    parser = argparse.ArgumentParser(description="Email merge for Grist")
    parser.add_argument("filename", metavar="FILENAME.YML")
    parser.add_argument("parameters", metavar="PAR", nargs="*")
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

    query = yaml_doc["query"].text

    if "parameters" in yaml_doc:
        nrequired = len(yaml_doc["parameters"])
        nsupplied = len(args.parameters)
        if nrequired != nsupplied:
            raise ValueError(
                f"{nrequired} parameters required, {nsupplied} supplied")

        param_values = {name.text: value
                        for name, value in zip(yaml_doc["parameters"],
                                               args.parameters,
                                               strict=True)}
        query = env.from_string(query).render(param_values)
    else:
        param_values = {}

    rows = client.sql(query)

    def expand_template(context, tpl):
        return env.from_string(tpl).render(context)

    subject_template = env.from_string(yaml_doc["subject"].text)
    body_template = env.from_string(yaml_doc["body"].text)

    table_to_inserts: Dict[str, List[Dict[str, Any]]] = {}
    updates: List[Tuple[str, Any]] = []

    for row in rows:
        # {{{ compute inserts

        if "insert" in yaml_doc:
            for insert_descr in yaml_doc["insert"]:
                tbl_name = insert_descr["table"].text
                new_record = {}
                for field, code in insert_descr["fields"].data.items():
                    globals = dict(row)
                    new_record[field] = exec_with_return(
                        code, f"<insert for '{tbl_name}.{field}'>", globals=globals)
                table_to_inserts.setdefault(tbl_name, []).append(new_record)
                if args.verbose or args.dry_run:
                    print(f"INSERT {insert_descr['table'].text}", new_record)

        # }}}

        # {{{ compute row updates

        row_updates: Dict[str, Any] = {}
        if "update" in yaml_doc:
            for field, code in yaml_doc["update"]["fields"].data.items():
                globals = dict(row)
                row_updates[field] = exec_with_return(
                    code, f"<update for '{field}'>", globals=globals)
        if row_updates:
            updates.append((row["id"], row_updates))

        if args.verbose or args.dry_run:
            print("UPDATE", row_updates)

        # }}}

        exp_context = param_values.copy()
        exp_context.update(row)
        for field, val in row_updates.items():
            exp_context[f"updated_{field}"] = val
        for table, inserts in table_to_inserts.items():
            for ins_num, insert in enumerate(inserts):
                for field, val in insert.items():
                    exp_context[f"inserted_{table}_{ins_num}_{field}"] = val

        subject, _ = re.subn(
            r"\s+", " ", subject_template.render(exp_context))
        body = body_template.render(exp_context).strip()

        expand = partial(expand_template, exp_context)

        msg = EmailMessage()
        msg["Subject"] = subject
        to = msg["To"] = convert_emails(expand, yaml_doc["to"])
        cc = msg["Cc"] = convert_emails(expand, yaml_doc["cc"])
        msg.set_content(body)

        if args.dry_run or args.verbose:
            print(75 * "#")
            print(f"Subject: {subject}")
            print(f"To: {', '.join(str(addr) for addr in to)}")
            print(f"Cc: {', '.join(str(addr) for addr in cc)}")
            print(30 * "-")
            print(body)

        if not args.dry_run:
            with Popen([args.sendmail, "-t", "-i"], stdin=PIPE) as p:
                p.communicate(msg.as_bytes())

    if table_to_inserts and not args.dry_run:
        for tbl, inserts in table_to_inserts.items():
            client.add_records(tbl, inserts)

    if updates and not args.dry_run:
        client.patch_records(
                yaml_doc["update"]["table"].text,
                updates)

# vim: foldmethod=marker
