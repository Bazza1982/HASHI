"""Derive the no-Python help cache from the actual parser; invoked by prepack."""
import argparse
import json
from pathlib import Path
from scripts.hashi_instance_cli import _parser


def export():
    documents = {}
    def visit(parser, prefix=''):
        documents[prefix] = parser.format_help()
        for action in parser._actions:
            if isinstance(action,argparse._SubParsersAction):
                for name, child in action.choices.items():
                    visit(child,(prefix+' '+name).strip())
    visit(_parser())
    from scripts.terminal_language import localize
    documents.update({'zh:' + topic:localize(page,'zh') for topic,page in list(documents.items())})
    return documents


if __name__ == '__main__':
    (Path(__file__).parent / 'terminal-help.json').write_text(json.dumps(export(),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
