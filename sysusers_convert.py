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
parser.add_argument('-p', '--permissive', action='store_true')
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
groupadd_parser.add_argument('-o', '--non-unique', action='store_true') # Ignored!
groupadd_parser.add_argument('name')

useradd_parser = argparse.ArgumentParser('useradd')
useradd_parser.add_argument('-u', '--uid')
useradd_parser.add_argument('-g', '--gid')
useradd_parser.add_argument('-G', '--groups')
useradd_parser.add_argument('-M', '--no-create-home', action='store_true')
useradd_parser.add_argument('-c', '--comment')
useradd_parser.add_argument('-r', '--system', action='store_true')
useradd_parser.add_argument('-d', '--home-dir')
useradd_parser.add_argument('-s', '--shell')
useradd_parser.add_argument('-k', '--skel')
useradd_parser.add_argument('-l', '--no-log-init', action='store_true') # Ignored
useradd_parser.add_argument('-o', '--non-unique', action='store_true') # Ignored!
useradd_parser.add_argument('-N', '--no-user-group', action='store_true')  # I think if we specify gid, this happens implicitly
useradd_parser.add_argument('-m', '--create-home', action='store_true')
useradd_parser.add_argument('name')

gpasswd_parser = argparse.ArgumentParser('gpasswd')
gpasswd_parser.add_argument('-a', '--add')
gpasswd_parser.add_argument('group')

usermod_parser = argparse.ArgumentParser('usermod')
usermod_parser.add_argument('-a', '--add', action='store_true')
usermod_parser.add_argument('-G', '--group')
usermod_parser.add_argument('-g', '--gid')
usermod_parser.add_argument('-d', '--home')
usermod_parser.add_argument('-l', '--login')
usermod_parser.add_argument('-L', '--lock', action='store_true')  # Ignored
usermod_parser.add_argument('name')

section_parser = argparse.ArgumentParser('section')
section_parser.add_argument('-n', action='store_true')
section_parser.add_argument('name', nargs='?')
section_parser.add_argument('-f', '--files')

def resolve_macro(specfile, macro):
    if macro is None or '%' not in macro:
        return macro
    f = tempfile.NamedTemporaryFile(prefix=specfile.stem, suffix='.spec', mode='w+t')
    f.write(specfile.read_text())
    f.write(f'\nMACRO: {macro}')
    f.flush()
    out = subprocess.check_output(['rpmspec', '-P', f.name], universal_newlines=True)
    blah = out.splitlines()[-1]
    assert blah.startswith('MACRO: ')
    return blah[7:]

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
    dprint(f'Converting {words}')
    for j,word in enumerate(words):
        if word.endswith(';'):
            words[j] = word[:-1]
            j += 1
            break
        elif '|' in word or '>' in word:
            break
    else:
        j += 1

    opts = parser.parse_args(words[1:j])
    return opts

SECTIONS = 'prep|build|check|package|description|files|pre|post|preun|postun|triggerun|triggerpostun|pretrans|posttrans|ldconfig_scriptlets|changelog'

@dataclasses.dataclass
class Section:
    where: slice
    opts: argparse.Namespace

def locate_section(specfile, lines, name, opts=None):
    beg = end = None
    for j,line in enumerate(lines):
        if beg is None and (m := re.match(fr'^%{name}(?:$|\s+)(.*)', line)):
            resolved = resolve_macro(specfile, m.group(1))
            ours = section_parser.parse_args(resolved.split())
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

    dprint(f'==== {specfile}')
    out_path = pathlib.Path(f'{specfile}.tmp')
    try:
        # remove .tmp file to not leave obsolete stuff in case we bail out
        os.unlink(out_path)
    except FileNotFoundError:
        pass

    name = dirname.absolute().name
    if name != specfile.stem:
        print('BAD SPEC FILE NAME')

    lines = open(specfile, 'rt').readlines()
    lines = [line.rstrip('\n') for line in lines]

    try:
        pre = locate_section(specfile, lines, 'pre')
    except ValueError:
        pre = None

    # find sysusers file and scriptlet
    sysusers_file = None
    groups = []
    users = []
    groups_where = []
    users_where = []

    for j,line in enumerate(lines):
        if not pre:
            break
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

        if m := re.search(r'\b(?:(?:/usr)?/sbin/|%{_sbindir})?(gpasswd\b.+)', line):
            dprint(f'matched {line}')
            new = parse_cmdline(gpasswd_parser, m.group(1))
            dprint(f'found gpasswd {new}')
            for user in users:
                if new.add == user.name:
                    assert not user.groups
                    user.groups = new.group
                    break
            else:
                assert opts.permissive

        if m := re.search(r'\b(?:(?:/usr)?/sbin/|%{_sbindir})?(usermod\b.+)', line):
            dprint(f'matched {line}')
            new = parse_cmdline(usermod_parser, m.group(1))
            dprint(f'found usermod {new}')
            if new.login:
                assert opts.permissive  # This is a rename, needs custom handling

            for user in users:
                if new.name == user.name:
                    if new.group:
                        user.groups = f'{user.groups},{new.group}' if user.groups else new.group
                    if new.gid:
                        assert user.gid is None
                        user.gid = new.gid
                    if new.home and new.home != user.home_dir:
                        assert user.home_dir is None
                        user.home_dir = new.home
                    break
            else:
                assert opts.permissive

    if not (groups or users or sysusers_file):
        raise Exception(f'{specfile}: cannot figure out scriplet')

    if pre:
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
        while re.match(r'(?:(?:/usr)?/s?bin/|%{_s?bindir})?(passwd\s+-l|usermod|groupmod|gpasswd)\b', lines[stop]):
            stop += 1
        if re.match(r'^(exit 0|:)$', lines[stop].strip()):
            stop += 1
        if start == pre.where.start + 1 and stop >= pre.where.stop:
            start -= 1
        elif lines[stop] == '':
            stop += 1
        del lines[start:stop]

    for group in groups:
        group.name_resolved = resolve_macro(specfile, group.name)

    for user in users:
        user.name_resolved = resolve_macro(specfile, user.name)
        user.gid_resolved = resolve_macro(specfile, user.gid)

    grumble = []
    if not sysusers_file:
        # Inject creation of the sysusers file
        prep = locate_section(specfile, lines, 'prep')

        inject = []
        for group in groups:
            if not group.system and not group.gid:
                grumble += ['Previously, a non-system group was created :(, sysusers does not support this.']

            if not any((group.name == user.name and group.gid == user.uid) or
                       (group.gid and group.gid == user.uid)
                       for user in users):
                inject += [f"g {group.name_resolved} {group.gid or '-'}"]

        for user in users:
            if user.shell and re.match('(?:(?:/usr)?/sbin|%{?_sbindir}?)/nologin$', user.shell):
                user.shell = None

            if not user.system and not user.uid:
                grumble += ['Previously, a non-system user was created :(, sysusers does not support this.']

            comment = repr(user.comment) if user.comment else '-'

            if user.create_home:
                grumble += ['Option -m was ignored. It must not be used for system users.']
            if user.skel:
                grumble += ['Option -k was ignored. It must not be used for system users.']

            uiditem = user.uid or '-'

            if (user.gid and
                user.gid_resolved != user.name_resolved and
                not any({user.gid, user.gid_resolved} & {group.name_resolved, group.gid}
                        for group in groups)):

                dprint('user:', user)
                dprint('groups:', groups)

                uiditem += f':{user.gid}'

            inject += [
                f"u {user.name_resolved} {uiditem} {comment} {user.home_dir or '-'} {user.shell or '-'}",
            ]

            extra_groups = [g for g in user.groups.split(',')
                            if g != user.name] if user.groups else []
            if extra_groups:
                inject += [f'm {user.name_resolved} {g}'
                           for g in extra_groups]

        lines[prep.where.stop:prep.where.stop] = [
            '',
            '# Create a sysusers.d config file',
            f'cat >{name.lower()}.sysusers.conf <<EOF',
            *inject,
            'EOF',
        ]

    # Remove Requires on shadow-utils
    to_remove = []
    for j,line in enumerate(lines):
        if m := re.match(r'(Requires(?:\(pre\))?:\s*)(.*(?:(useradd|groupadd|getent|shadow-utils).*))', line):
            args = m.group(2).split()
            filtered = [arg for arg in args
                        if not re.match('((?:(?:/usr)?/s?bin|%{?_s?bindir}?)/(useradd|groupadd|getent)|shadow-utils),?$', arg)]
            if filtered:
                if filtered != args:
                    lines[j] = m.group(1) + ' '.join(filtered).rstrip(',')
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
        install = locate_section(specfile, lines, 'install')
        lines[install.where.stop:install.where.stop] = [
            '',
            f'install -m0644 -D {name.lower()}.sysusers.conf %{{buildroot}}%{{_sysusersdir}}/{name.lower()}.conf',
        ]

        # Inject sysusers file into %files
        files = locate_section(specfile, lines, 'files', pre.opts)
        lines[files.where.stop:files.where.stop] = [
            f'%{{_sysusersdir}}/{name.lower()}.conf',
        ]

    # write stuff out and diff
    with open(out_path, 'wt') as out:
        print('\n'.join(lines), file=out)

    grumble = sorted(set(grumble))

    if groups or users:
        CHLOG = 'Add sysusers.d config file to allow rpm to create users/groups automatically'
        COMMENT = '\n'.join((CHLOG,
                             '',
                             'See https://fedoraproject.org/wiki/Changes/RPMSuportForSystemdSysusers.',
                             *grumble))
    else:
        CHLOG = 'Drop call to %sysusers_create_compat'
        COMMENT = '\n'.join((CHLOG,
                             '',
                             'After https://fedoraproject.org/wiki/Changes/RPMSuportForSystemdSysusers,',
                             'rpm will handle account creation automatically.',
                             *grumble))

    if opts.bumpspec:
        cmd = ['rpmdev-bumpspec', '-c', CHLOG, out_path]
        if opts.user:
            cmd += ['-u', opts.user]
        subprocess.check_call(cmd)

    if opts.diff:
        subprocess.call([
            'git',
            '--no-pager',
            '-c', 'color.diff=always',
            'diff',
            f'-U{opts.U}',
            '--no-index',
            specfile,
            out_path,
        ])

    if opts.write:
        out_path.rename(specfile)

    if opts.commit:
        subprocess.check_call([
            'git',
            f'--git-dir={dirname}/.git',
            f'--work-tree={dirname}/',
            'commit',
            '-a',
            '-m', COMMENT,
        ])
