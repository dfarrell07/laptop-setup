"""Conservative checksum exceptions for the public Go tools installed by this role."""
import re

PUBLIC_PROJECT_HOSTS = ('golang.org', 'mvdan.cc', 'sigs.k8s.io', 'k8s.io', 'gopkg.in')
PUBLIC_HOSTS = ('github.com',) + PUBLIC_PROJECT_HOSTS
SUPPORTED_PATTERN = re.compile(r'[A-Za-z0-9._*~+-]+(?:/[A-Za-z0-9._*~+-]+)*')


def prefix_matches(pattern, target):
    """Go MatchPrefixPatterns semantics for our supported literal/asterisk subset."""
    components = pattern.count('/') + 1
    target_parts = target.split('/')
    if len(target_parts) < components:
        return False
    prefix = '/'.join(target_parts[:components])
    expression = re.escape(pattern).replace(r'\*', '[^/]*')
    return re.fullmatch(expression, prefix) is not None


def go_checksum_exceptions_safe(patterns, installed_modules):
    if not isinstance(patterns, str):
        return False
    targets = [module.split('@', 1)[0] for module in installed_modules]
    for pattern in patterns.split(','):
        # Go trims one trailing slash and ignores empty entries.
        pattern = pattern.removesuffix('/')
        if not pattern:
            continue
        # Refuse syntax we do not interpret rather than silently passing a glob.
        if not SUPPORTED_PATTERN.fullmatch(pattern):
            return False
        parts = pattern.split('/')
        public_hosts = [host for host in PUBLIC_HOSTS if prefix_matches(parts[0], host)]
        if public_hosts:
            # These project hosts serve public modules, including dependencies of
            # our tools. A narrower path there is not a private-owner exception.
            if any(host in PUBLIC_PROJECT_HOSTS for host in public_hosts):
                return False
            # Whole public hosts and wildcard owners are too broad. Literal
            # private owners remain usable unless they cover a managed public tool.
            if '*' in parts[0] or len(parts) == 1 or '*' in parts[1]:
                return False
            if any(target.split('/')[0] in PUBLIC_HOSTS and prefix_matches(pattern, target)
                   for target in targets):
                return False
    return True


class FilterModule:
    def filters(self):
        return {'go_checksum_exceptions_safe': go_checksum_exceptions_safe}
