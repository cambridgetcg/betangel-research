"""Deny the backtester every path inside the Betfair client's live-state or default client-certificate trees (D12).

AGENTS.md: the backtester must not access account profiles or live order state, and the client keeps its live order
state and its default client certificate outside every Git worktree, in two fixed trees under the operator's home. The
runner takes only explicit paths, never derives one from the home directory and never reads the client's
configuration, so it cannot know where an operator put a custom certificate; what it can do is refuse the two fixed
trees wherever they are reached. guard_path refuses a path when the casefolded components of its absolute form, or
of its resolved target, contain any DENIED_COMPONENT_SEQUENCES entry as consecutive components (casefolded, because
the default macOS file system ignores case). The resolved check catches a symlink anywhere on the way, a child
symlink under a permitted root included, so every derived input, output and ledger path is guarded too, not just
the roots. A path that cannot be inspected or resolved is refused, never let through.

An existing path (prospective False) must resolve strictly. An output that does not exist yet (prospective True) is
checked by resolving its parent strictly and appending its name; the parent must exist and be a directory, the name
must be one plain component, and nothing may be there yet (not even a dangling symlink, which would redirect the
write elsewhere): already_exists.

What this establishes, and what it does not: the two fixed trees are refused for any path given to guard_path,
before anything opens, lists or creates it. Outside it: a custom certificate location elsewhere; a hard link to a
file in a denied tree (a path cannot show it); a tree the operator relocated so that its real path no longer holds
the sequence; a directory that merely contains a denied tree, such as a home directory (the caller guards each path
it derives before listing or opening it, never only a root); resolution itself, which inspects the symlinks it
follows, inside a denied tree included, before refusing; a change of the filesystem between the check and the use
(the caller keeps exclusive, no-follow creation and its own no-swap controls); and secrets in operator-supplied
historical files. It is a denial list, not operating-system isolation or a secret detector. Other modules use only
guard_path, PathDenied and denied_sequence (the boundary test checks that none reads the deny list itself). The boundary test allows the protected names in this module only inside the one top-level
DENIED_COMPONENT_SEQUENCES assignment, whose exact value it checks; every other string, import and home-directory
check still applies here. Standard library only (pathlib); nothing here touches the home directory.
"""

from pathlib import Path

DENIED_COMPONENT_SEQUENCES = (("Library", "Application Support", "betangel-research", "betfair"),
                              (".config", "betangel-research", "betfair"))


class PathDenied(Exception):
    """A path the backtester may not use; str() is "<where>:<reason>"."""


def _folded(sequences):
    return tuple(tuple(part.casefold() for part in sequence) for sequence in sequences)


_DENIED = _folded(DENIED_COMPONENT_SEQUENCES)


def denied_sequence(path):
    """True when the components of path (as given, made absolute without resolving) contain a denied sequence."""
    parts = tuple(part.casefold() for part in Path(path).absolute().parts)
    for sequence in _DENIED:
        width = len(sequence)
        if any(parts[start:start + width] == sequence for start in range(len(parts) - width + 1)):
            return True
    return False


def guard_path(path, where, prospective=False):
    """The resolved Path of path after the checks in the module docstring; PathDenied("<where>:<reason>") with reason
    denied_live_state_tree (the given or the resolved form names a denied tree), unresolvable (it, or for a
    prospective path its parent, cannot be inspected or resolved), parent_not_a_directory or not_one_name (a
    prospective path whose parent is not a directory or whose name is not one plain component), already_exists (a
    prospective path where something, a dangling symlink included, already is), or missing_path (prospective False
    and nothing there)."""
    if path is None or not str(path).strip():
        raise PathDenied(f"{where}:missing_path")
    try:
        given = Path(path)
        if denied_sequence(given):
            raise PathDenied(f"{where}:denied_live_state_tree")
        if prospective:
            name = given.name
            if name in ("", ".", "..") or given.parent == given:
                raise PathDenied(f"{where}:not_one_name")
            parent = given.parent.resolve(strict=True)
            if not parent.is_dir():
                raise PathDenied(f"{where}:parent_not_a_directory")
            resolved = parent / name
            try:
                resolved.lstat()
            except FileNotFoundError:
                pass
            else:
                raise PathDenied(f"{where}:already_exists")
        else:
            resolved = given.resolve(strict=True)
    except FileNotFoundError:
        raise PathDenied(f"{where}:{'unresolvable' if prospective else 'missing_path'}") from None
    except (OSError, RuntimeError, ValueError):            # ValueError: an embedded NUL, say
        raise PathDenied(f"{where}:unresolvable") from None
    if denied_sequence(resolved):
        raise PathDenied(f"{where}:denied_live_state_tree")
    return resolved
