#!/usr/bin/python3

import argparse
import collections
import dataclasses
import os
import pathlib
import re
import shlex
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--diff', action='store_true')
parser.add_argument('-d', '--debug', action='store_true')
parser.add_argument('-U', type=int, default=3)
parser.add_argument('-b', '--bumpspec', action='store_true')
parser.add_argument('-c', '--commit', action='store_true')
parser.add_argument('-u', '--user')
parser.add_argument('-w', '--write', action='store_true')
parser.add_argument('dirname', type=pathlib.Path, nargs='+')
opts = parser.parse_args()

groupadd_parser = argparse.ArgumentParser('groupadd')
groupadd_parser.add_argument('-g', '--gid')
groupadd_parser.add_argument('-f', '--force', action='store_true')
groupadd_parser.add_argument('-r', '--system', action='store_true')
groupadd_parser.add_argument('name')

useradd_parser = argparse.ArgumentParser('useradd')
useradd_parser.add_argument('-u', '--uid')
useradd_parser.add_argument('-g', '--gid')
useradd_parser.add_argument('-G', '--groups')
useradd_parser.add_argument('-M', '--no-create-home', action='store_true')
useradd_parser.add_argument('-c', '--comment')
useradd_parser.add_argument('-r', '--system', action='store_true')
useradd_parser.add_argument('-d', '--directory')
useradd_parser.add_argument('-s', '--shell')
useradd_parser.add_argument('-l', '--no-log-init', action='store_true') # Ignored
useradd_parser.add_argument('-o', '--non-unique', action='store_true') # Ignored!
useradd_parser.add_argument('name')

section_parser = argparse.ArgumentParser('section')
section_parser.add_argument('-n', action='store_true')
section_parser.add_argument('name', nargs='?')
section_parser.add_argument('-f', '--files')

def resolve_macro(specfile, macro):
    f = tempfile.NamedTemporaryFile(prefix=specfile.stem, suffix='.spec', mode='w+t')
    f.write(specfile.read_text())
    f.write(f'\nMACRO: {macro}')
    f.flush()
    out = subprocess.check_output(['rpmspec', '-P', f.name], universal_newlines=True)
    blah = out.splitlines()[-1]
    assert blah.startswith('MACRO: ')
    return blah[7:]

def is_requires(type, pattern, line):
    return re.match(rf'{type}:\s*({pattern})', line) is not None

def dprint(*args, **kwargs):
    if opts.debug:
        print(*args, **kwargs)

def logical_line(lines, j):
    if lines[j-1].endswith('\\'):
        return None, None
    end = j
    while lines[end].endswith('\\'):
        end += 1
    return (''.join([lines[i][:-1] for i in range(j, end)] + [lines[end]]),
            slice(j, end + 1))

def parse_cmdline(parser, line):
    words = shlex.split(line)
    print(f'Converting {words}')
    for j,word in enumerate(words):
        if '|' in word or '>' in word:
            break
    else:
        j += 1

    opts = parser.parse_args(words[1:j])
    return opts

SECTIONS = 'prep|build|check|package|description|files|pre|post|preun|postun|triggerun|triggerpostun|ldconfig_scriptlets|changelog'

@dataclasses.dataclass
class Section:
    where: slice
    opts: argparse.Namespace

def locate_section(lines, name, opts=None):
    beg = end = None
    for j,line in enumerate(lines):
        if beg is None and re.match(fr'%{name}\b', line):
            ours = section_parser.parse_args(line.split()[1:])
            if opts and ours.name != opts.name:
                continue
            beg = j
        elif beg is not None and re.match(rf'%({SECTIONS})\b', line):
            end = j - 1
            break

    if not end:
        raise ValueError(f"Cannot find section %{name} {opts or ''}")

    while end > beg and not lines[end-1]:
        end -= 1

    return Section(slice(beg, end), ours)

for dirname in opts.dirname:
    if dirname.name.endswith('.spec'):
        dirname = dirname.parent

    specfile, = dirname.glob('*.spec')

    print(f'==== {specfile}')
    out_path = pathlib.Path(f'{specfile}.tmp')
    try:
        # remove .tmp file to not leave obsolete stuff in case we bail out
        os.unlink(out_path)
    except FileNotFoundError:
        pass

    name = dirname.name
    if name != specfile.stem:
        print('BAD SPEC FILE NAME')

    lines = open(specfile, 'rt').readlines()
    lines = [line.rstrip('\n') for line in lines]

    pre = locate_section(lines, 'pre')

    # find sysusers file and scriptlet
    sysusers_file = None
    groups = []
    users = []
    groups_where = []
    users_where = []

    for j,line in enumerate(lines):
        if not (pre.where.start < j <= pre.where.stop):
            continue

        line, where = logical_line(lines, j)
        if line is None:
            continue

        if m := re.match(r'%{?sysusers_create_compat}?\s+(.*)$', line):
            dprint(f'matched {line}')
            assert not sysusers_file
            sysusers_file = m.group(1)
            sysusers_compat_where = where

        if m := re.search(r'\b(?:(?:/usr)?/sbin/|%{_sbindir})?(groupadd\b.+)', line):
            dprint(f'matched {line}')
            new = parse_cmdline(groupadd_parser, m.group(1))
            dprint(f'found groupadd {new}')
            groups += [new]
            groups_where += [where]

        if m := re.search(r'\b(?:(?:/usr)?/sbin/|%{_sbindir})?(useradd\b.+)', line):
            dprint(f'matched {line}')
            new = parse_cmdline(useradd_parser, m.group(1))
            dprint(f'found useradd {new}')
            users += [new]
            users_where += [where]

    if not (groups or users or sysusers_file):
        raise Exception('cannot figure out scriplet')

    start = min(*(where.start for where in groups_where),
                *(where.start for where in users_where),
                sysusers_compat_where.start if sysusers_file else 1e9,
                1e9)
    stop = max(*(where.stop for where in groups_where),
               *(where.stop for where in users_where),
               sysusers_compat_where.stop if sysusers_file else 0,
               0)

    while start > pre.where.start + 1 and re.match(r'^(#.*|)$', lines[start-1]):
        start -= 1
    if re.match(r'(?:(?:/usr)?/bin/|%{_bindir})?passwd\s+-l\s+', lines[stop]):
        stop += 1
    while re.match(r'^(exit 0|)$', lines[stop]):
        stop += 1
    if start == pre.where.start + 1 and stop >= pre.where.stop:
        start -= 1
    elif lines[stop] == '':
        stop += 1
    del lines[start:stop]

    for group in groups:
        if '%' in group.name: # Jesus, have mercy
            verbatim = resolve_macro(specfile, group.name)
            group.name = verbatim

        assert group.system or group.gid
    for user in users:
        if '%' in user.name: # Jesus, have mercy
            verbatim = resolve_macro(specfile, user.name)
            user.name = verbatim

        assert user.system or user.uid, user
        assert user.gid is None or any(user.gid in (group.name, group.gid) for group in groups), (user, groups)

    if not sysusers_file:
        # Inject creation of the sysusers file
        prep = locate_section(lines, 'prep')

        inject = []
        for group in groups:
            if not any(group.name == user.name or (group.gid and group.gid == user.uid)
                       for user in users):
                inject += [f"g {group.name} {group.gid or '-'}"]

        for user in users:
            if re.match('(?:(?:/usr)?/sbin|%{?_sbindir}?)/nologin$', user.shell):
                user.shell = None

            comment = repr(user.comment) if user.comment else '-'

            inject += [
                f"u {user.name} {user.uid or '-'} {comment} {user.directory} {user.shell or '-'}",
            ]

            extra_groups = [g for g in user.groups.split(',')
                            if g != user.name] if user.groups else []
            if extra_groups:
                inject += [f'm {user.name} {g}'
                           for g in extra_groups]

        lines[prep.where.stop:prep.where.stop] = [
            '',
            '# Create a sysusers.d config file',
            f'cat >{name}.sysusers.conf <<EOF',
            *inject,
            'EOF',
        ]

    # Remove Requires on shadow-utils
    to_remove = []
    for j,line in enumerate(lines):
        if m := re.match(r'(Requires(?:\(pre\))?:\s*)(.*(?:(useradd|groupadd|shadow-utils).*))', line):
            args = m.group(2).split()
            filtered = [arg for arg in args
                        if not re.match('((?:(?:/usr)?/sbin|%{?_sbindir}?)/(useradd|groupadd)|shadow-utils)$', arg)]
            if filtered:
                if filtered != args:
                    lines[j] = m.group(1) + ' '.join(filtered)
                else:
                    print('Keeping', line)
            else:
                to_remove += [j]
        elif re.match(r'%{?\??sysusers_requires_compat}?', line):
            to_remove += [j]

    for j in reversed(to_remove):
        del lines[j]

    # Inject installation
    if not sysusers_file:
        install = locate_section(lines, 'install')
        lines[install.where.stop:install.where.stop] = [
            '',
            f'install -m0644 -D {name}.sysusers.conf %{{buildroot}}%{{_sysusersdir}}/{name}.conf',
        ]

        # Inject sysusers file into %files
        files = locate_section(lines, 'files', pre.opts)
        lines[files.where.stop:files.where.stop] = [
            f'%{{_sysusersdir}}/{name}.conf',
        ]

    # write stuff out and diff
    with open(out_path, 'wt') as out:
        print('\n'.join(lines), file=out)

    COMMENT = ('Add sysusers.d config file\n'
               '  See https://fedoraproject.org/wiki/Changes/RPMSuportForSystemdSysusers')

    if opts.bumpspec:
        cmd = ['rpmdev-bumpspec', '-c', COMMENT, out_path]
        if opts.user:
            cmd += ['-u', opts.user]
        subprocess.check_call(cmd)

    if opts.diff:
        subprocess.call(['git',
                         # '--no-pager',
                         'diff',
                         f'-U{opts.U}',
                         '--no-index',
                         specfile,
                         out_path])

    if opts.write:
        out_path.rename(specfile)

    if opts.commit:
        subprocess.check_call(['git',
                               f'--git-dir={dirname}/.git',
                               f'--work-tree={dirname}/',
                               'commit', '-a',
                               '-m', COMMENT.split('\n')[0]])
