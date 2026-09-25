"""Where command modules attach their subcommands to the dava argument parser."""

import argparse


class Registry:
    """Hands out command groups ('timeline', 'render', ...) so several modules can add actions to one group.

    Every leaf parser should call set_defaults(func=HANDLER, read_only=BOOL_OR_CALLABLE).
    read_only=True marks a command that can never change Resolve state; a callable receives the
    parsed args and decides (e.g. 'page' only reads when no page name is given). Commands without
    read_only are treated as state-changing, which is the safe default for --read-only mode.
    """

    def __init__(self, commands):
        self.commands = commands  # the top-level subparsers action
        self.groups = {}

    def group(self, name, help=None):
        """Return the subparsers action of command group `name`, creating the group on first use."""
        if name not in self.groups:
            if name in self.commands.choices:
                raise ValueError(f"'{name}' is already a plain command, not a group.")
            parser = self.commands.add_parser(name, help=help)
            self.groups[name] = parser.add_subparsers(dest="action", required=True, metavar="ACTION")
        return self.groups[name]

    def command(self, name, help, **kwargs):
        """Add a top-level command without actions and return its parser."""
        if name in self.commands.choices:
            raise ValueError(f"Command '{name}' is defined twice.")
        return self.commands.add_parser(name, help=help, **kwargs)

    def action(self, group, name, help, group_help=None, **kwargs):
        """Add action `name` to `group` and return its parser; refuses duplicates."""
        actions = self.group(group, group_help)
        if name in actions.choices:
            raise ValueError(f"'{group} {name}' is defined twice.")
        return actions.add_parser(name, help=help, **kwargs)


def leaf_parsers(parser, prefix=(), help_text=None):
    """Yield (command words, parser, help text) for every runnable command under parser."""
    subparsers = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subparsers:
        yield prefix, parser, help_text
        return
    for action in subparsers:
        helps = {choice.dest: choice.help for choice in action._choices_actions}
        for name, child in action.choices.items():
            yield from leaf_parsers(child, prefix + (name,), helps.get(name))


def all_parsers(parser):
    """Yield parser and every sub-parser below it."""
    yield parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                yield from all_parsers(child)
