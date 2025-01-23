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
    f = tempfile.NamedTemporaryFile(prefix=specfile.stem, suffix='.spec', mode='w+t', delete=False)
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

SECTIONS = 'prep|build|check|package|description|files|pre|post|preun|postun|triggerun|triggerpostun|ldconfig_scriptlets'

@dataclasses.dataclass
class Section:
    where: slice
    args: argparse.Namespace

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
        raise ValueError(f"Cannot find section %{name} {' '.join(args)}")

    while end > beg and not lines[end-1]:
        end -= 1

    return Section(slice(beg, end), ours)

for dirname in opts.dirname:
    if dirname.name.endswith('.spec'):
        dirname = dirname.parent

    specfile, = dirname.glob('*.spec')

    print(f'==== {specfile}')
    new = pathlib.Path(f'{specfile}.tmp')
    try:
        # remove .tmp file to not leave obsolete stuff in case we bail out
        os.unlink(new)
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
    group = None
    user = None
    for j,line in enumerate(lines):
        if not (pre.where.start < j <= pre.where.stop):
            continue

        line, where = logical_line(lines, j)
        if line is None:
            continue

        if m := re.match(r'%{?_sysusersdir}?/.*\.conf', line):
            sysusers_file = lines[j]
            print(f'found sysusers {sysusers_file}')

        if m := re.search(r'\b(?:(?:/usr)?/sbin/|%{_sbindir})?(groupadd\b.+)', line):
            dprint(f'matched {line}')
            assert not group
            group = parse_cmdline(groupadd_parser, m.group(1))
            dprint(f'found groupadd {group}')
            group_where = where

        if m := re.search(r'\b(?:(?:/usr)?/sbin/|%{_sbindir})?(useradd\b.+)', line):
            dprint(f'matched {line}')
            assert not user
            user = parse_cmdline(useradd_parser, m.group(1))
            dprint(f'found useradd {user}')
            user_where = where

        # Got everything we care about
        # if sysusers_file and group and user:
        #     break

    if not group and not user:
        raise Exception('cannot figure out scriplet')

    if sysusers_file:
        continue

    start = min(group_where.start if group else 1e9,
                user_where.start if user else 1e9)
    stop = max(group_where.stop if group else 0,
               user_where.stop if user else 0)
    if lines[stop].strip() == 'exit 0':
        stop += 1
    if start == pre.where.start + 1 and stop == pre.where.stop:
        start -= 1
    elif lines[stop] == '':
        stop += 1
    if lines[start - 1] == '':
        start -= 1
    del lines[start:stop]

    if group:
        assert group.system or group.gid
    if user:
        assert user.system or user.uid
        assert user.gid is None or (group and user.gid == group.name)

    # Inject creation of the sysusers file
    prep = locate_section(lines, 'prep')

    if re.match('(?:(?:/usr)?/sbin|%{?_sbindir}?)/nologin$', user.shell):
        user.shell = None

    if group and (group.name != user.name or (group.gid and group.gid != user.uid)):
        group_line = [f"g {group.name} {group.gid or '-'}"]
    else:
        group_line = []

    comment = repr(user.comment) if user.comment else '-'

    if user.groups:
        extra_lines = [f'm {user.name} {g}'
                       for g in user.groups.split(',')
                       if g is not user.name]
    else:
        extra_lines = []

    lines[prep.where.stop:prep.where.stop] = [
        '',
        '# Create a sysusers.d config file',
        f'cat >{name}.sysusers.conf <<EOF',
        *group_line,
        f"u {user.name} {user.uid or '-'} {comment} {user.directory} {user.shell or '-'}",
        *extra_lines,
        'EOF',
    ]

    to_remove = []
    for j,line in enumerate(lines):
        if m := re.match(f'(Requires(?:\(pre\))?:\s*)(.*(?:(useradd|groupadd|shadow-utils).*))', line):
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
    for j in reversed(to_remove):
        del lines[j]

    # Inject installation
    install = locate_section(lines, 'install')
    lines[install.where.stop:install.where.stop] = [
        '',
        f'install -m0644 -D {name}.sysusers.conf %{{buildroot}}%{{_sysusersdir}}/{name}.conf',
    ]

    # Inject sysusers file into %files
    files = locate_section(lines, 'files', pre.args)
    lines[files.where.stop:files.where.stop] = [
        f'%{{_sysusersdir}}/{name}.conf',
    ]

    # write stuff out and diff
    with open(new, 'wt') as out:
        print('\n'.join(lines), file=out)

    COMMENT = ('Add sysusers.d config file\n'
               '  See https://fedoraproject.org/wiki/Changes/RPMSuportForSystemdSysusers')

    if opts.bumpspec:
        cmd = ['rpmdev-bumpspec', '-c', COMMENT, new]
        if opts.user:
            cmd += ['-u', opts.user]
        subprocess.check_call(cmd)

    if opts.diff:
        subprocess.call(['git', 'diff', f'-U{opts.U}', '--no-index', specfile, new])

    if opts.write:
        new.rename(specfile)

    if opts.commit:
        subprocess.check_call(['git',
                               f'--git-dir={dirname}/.git',
                               f'--work-tree={dirname}/',
                               'commit', '-a',
                               '-m', COMMENT.split('\n')[0]])
