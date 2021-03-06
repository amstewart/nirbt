#!/usr/bin/python
"""This utility enwraps the process of creating and updating review-board review
requests for git commits at National Instruments. It can (will someday) also
enwrap the process of updating git commits to add Acked-by, Review-board ID, and
CAR-id lines.

Maintainer: Alex Stewart <alex.stewart@ni.com>
"""

import argparse
import ConfigParser
import json
import re
import os
from subprocess import call
import string
import sys
import tempfile
import webbrowser
import pycurl
import pygit2
from enum import Enum
from rbtools.api.client import RBClient

# <constants>
RE_NI_GIT = re.compile(r'git\.natinst\.com:?/(.+)\.git')
RE_UPSTREAM = re.compile(r'refs/remotes/\w+/(.*)')
PATH_CONFIG = os.path.join(os.path.expanduser("~"), ".config", "nirbt.conf")
# </constants>

# <typedefs>
class CHAN(Enum):
    """Writeout output channels"""
    NORMAL = 0
    VERBOSE = 1
    ERROR = 2

class Settings:
    """Instance settings"""
    client = None
    commits = []
    commit_start = None
    commit_end = None
    config = None
    dry_run = False
    local_repo = None
    rb_repo = None
    verbose = False
# </typedefs>

# <globals>
settings = Settings()
# </globals>

def main(args):
    if not bootstrap(args):
        return 1
    # settings.rb_repo now contains a validated review-board repository object
    # corresponding as best as we can determine to the local repo that the user
    # is running from.

    # call whatever command was specified by the CLI args
    # will either branch to command_upload or command_update
    args.func(args)
    return 0

def bootstrap(args):
    """Evaluates command line arguments and configuration files and sets the
    global 'settings' object accordingly. Requests the list of valid
    repositories from the configured Review-Board server and confirms that the
    current git repository has one of those repos as a remote. Selects one of
    those valid repos as the settings.rb_repo.

    Returns: True, if all operations were successful and a validated repo was
             set; False, otherwise
    """
    # parse cli args
    eval_args(args)

    # setup config
    config_new = ConfigParser.SafeConfigParser()
    successful_files = config_new.read(PATH_CONFIG)
    if not successful_files:
        raise Exception("No configuration files found.")
    else:
        writeout(CHAN.VERBOSE, "Parsed files:\n")
        for config_file in successful_files:
            writeout(CHAN.VERBOSE, "\t%s\n", config_file)
    settings.config = config_new

    settings.client = RBClient(settings.config.get('NATI', 'server'),
                               token=settings.config.get('NATI', 'token'))

    # discover repo and load into settings.local_repo
    if not discover_repo(os.getcwd()):
        return False
    # query the review-board server to check for the repository
    if not validate_repo(settings.local_repo):
        return False

    return True

def command_amend(config):
    """Fetches a specified review request from the RB server and parses it for
    its diff and comments. Based on the diff, determines which commit(s) from
    the working repository are being reviewed. Then amends the commit messages
    for those commits based on the request comments. (Adding "Acked-by" and
    "Reviewed-by" lines as appropriate, consistent with NI practices.
    config : ConfigParser object of the current configuration
    
    Returns: True, if all actions succeeded; False, otherwise
    """
    writeout(CHAN.ERROR, "Command not implemented.")
    pass

def command_update(config):
    """Performs the same actions as command_upload, except updates an existing
    review request.
    config : ConfigParser object of the current configuration
    
    Returns: True, if all actions succeeded; False, otherwise
    """
    writeout(CHAN.ERROR, "Command not implemented.")
    pass

def command_upload(config):
    """Creates a diff of the latest commit in the current working git branch and
    parses the commit message for its contents. Then creates a new review
    request on the Review-Board server and pushes the diff, summary,
    description, and tracking branch to its draft. Then opens the draft for the
    user to complete the remainder of the request and publish.
    config : ConfigParser object of the current configuration

    Returns: True, if all actions succeeded; False, otherwise
    """
    # TODO: Extend this functionality to upload additional commits as a batch.
    writeout(CHAN.VERBOSE, "\nGathering information for request...\n")

    if not settings.commit_start:
        settings.commit_start = "HEAD"
    writeout(CHAN.NORMAL, "HEAD STRING=%s\n", settings.commit_start)

    tracking=None
    for branch in settings.local_repo.listall_branches(pygit2.GIT_BRANCH_LOCAL):
        branch = settings.local_repo.lookup_branch(branch)
        if branch.is_head():
            try:
                match = RE_UPSTREAM.search(branch.upstream_name)
                if match:
                    tracking = match.group(1)
            except Exception:
                pass
    writeout(CHAN.VERBOSE, "\tTracking = %s\n", tracking)


    # Get the commits that (hopefully) are the same as git-diff just created
    # the diff for. Concat their commit messages into fp_description
    fp_description = tempfile.TemporaryFile()
    (commits, commit_prev) = get_commits(settings.local_repo,
                                         start=settings.commit_start,
                                         end=settings.commit_end)
    for commit in commits:
        fp_description.write("%s - %s\n" % (commit.author.name, commit.id))
        fp_description.write("%s\n" % commit.message)
        fp_description.write("-----\n")
    writeout(CHAN.VERBOSE, "\tDescription is %d bytes.\n",
             fp_description.tell())
    fp_description.seek(0)
    if settings.dry_run:
        writeout(CHAN.VERBOSE, "\tDescription=\n")
        for line in fp_description.readlines():
            writeout(CHAN.VERBOSE, "\t%s", line)

        
    # We have to call 'git diff' here because the pygit2 API doesn't have the
    # switch for --full-index yet.
    fp_diff = tempfile.TemporaryFile()
    call(['git', 'diff', '--full-index', str(commit_prev.id),
          str(commits[0].id)], stdout=fp_diff)
    writeout(CHAN.VERBOSE, "\tDiff is %d bytes.\n", fp_diff.tell())
    fp_diff.seek(0)
    if settings.dry_run:
        writeout(CHAN.VERBOSE, "\tDiff=\n")
        for line in fp_diff.readlines():
            writeout(CHAN.VERBOSE, "\t%s", line)

    # Now put the first line of the latest commit into the summary string
    summary = ""
    summary = commits[0].message.splitlines()[0]
    writeout(CHAN.VERBOSE, "\tSummary = %s\n", summary)

    repo_id = settings.rb_repo['id']
    review_request = None
    if not settings.dry_run:
        review_request = settings.client.get_root().get_review_requests()\
                         .create(repository=repo_id)
        if not review_request:
            writeout(CHAN.ERROR, "Could got create review request.\n")
            return False
        else:
            writeout(CHAN.VERBOSE, "Created review request #%d\n",
                     review_request.id)

        review_request.get_diffs().upload_diff(fp_diff.read())
        draft = review_request.get_draft()
        draft = draft.update(summary=summary, description=fp_description.read())
        if tracking:
            draft = draft.update(branch=tracking)
        user = settings.client.get_root().get_session().get_user()
        draft = draft.update(target_people=user.username)
        webbrowser.open(review_request.absolute_url)

    return True

def discover_repo(repo_path):
    """Searches an arbitrary local directory for a git repository
    repo_path : string path to search
    
    Returns: True, if a git repository is found; False, otherwise
    """
    try:
        writeout(CHAN.VERBOSE, "Searching location %s for git repo...\n",
                 repo_path)
        repo_path = pygit2.discover_repository(repo_path)
        writeout(CHAN.VERBOSE, "Found!\n")
        repo = pygit2.Repository(repo_path)
        settings.local_repo = repo
        return True
    except Exception as e:
        writeout(CHAN.ERROR, "No repository found at %s\n", e)
        return False

def eval_args (args):
    """Converts the argument namespace parsed by ArgParser to the settings
    global.
    args: ArgParser argument namespace
    
    Returns: True
    """
    if args.verbose:
        settings.verbose = True
    if args.dry_run:
        settings.dry_run = True
    if args.commits:
        test = string.split(args.commits, '..', 2)
        try:
            settings.commit_start = test[0]
            settings.commit_end = test[1]
        except IndexError:
            pass
    return True

def get_commits(repo, start="HEAD", end=None):
    """Walks the current repository's commit log, beginning with the git object
    alluded to by the 'start' string and ending with the optional 'end' string.
    repo  : pygit2.repository object of the local repository
    start : a string, parsable by git, alluding to the first commit to return
    end   : a string, parsable by git, alluding to the last commit to return;
            defaults to the same value as 'start', if unasserted

    Returns: A tuple who's first element is a list of all commit objects between
    the start and end commits, inclusive. The second element is the parent
    commit of the 'end' object, or None if it has no parent.
    Throws: ValueError, when provided invalid 'start' or 'end' strings;
            pygit2.GitError, when internal git errors occur
    """
    git_walk_order = (pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_TIME)

    commit_start = repo.revparse_single(start)
    if not commit_start:
        raise ValueError("Invalid starting commit: %s" % start)

    if not end:
        end = start
    commit_end = repo.revparse_single(end)
    if not commit_end:
        raise ValueError("Invalid ending commit: %s" % end)
    
    # store new commits into a temp variable, so that we don't blast the
    # global commits if we error out here
    commits = []
    try:
        for commit in settings.local_repo.walk(commit_start.id, git_walk_order):
            commits.append(commit)
            writeout(CHAN.VERBOSE, "++COMMIT = %s : %s\n", commit.author.name,
                     commit.id)
            if commit.id == commit_end.id:
                break
    except pygit2.GitError as e:
        writeout(CHAN.ERROR, "Could not get commits. " \
                             "Probably no commits in repo.\n")
        return None
    return (commits, repo.revparse_single(end + "^"))

def pick_repo(selection, rb_repos):
    """Presents the user with all of the repositories known to the RB server
    which are also configured as remotes for the local repository (see:
    selection). Selects the first viable repo from that set as the 'validated
    repo' and returns its id.
    selection : Set() of repo names which are the intersection of the local
                remotes and the RB server's repos.
    rb_repos : List of Dicts of the RB servers repository information
    
    Returns: The id of the 'validated repo'
    """
    if len(selection) is 0:
        writeout(CHAN.ERROR, "No repository(s) that match found on " \
                 "review-board server: %s\n", server)
    else:
        writeout(CHAN.NORMAL, "Found matching repositories:\n\n")

    #repo_opthions = set()
    for rb_repo in rb_repos:
        r_name = rb_repo['name']
        if unicode(r_name) in selection:
            r_id = rb_repo['id']
            r_tool = rb_repo['tool']
            r_path = rb_repo['path']
            writeout(CHAN.NORMAL, "[%s] %s (%s) <- %s\n",
                     r_id, r_name, r_tool, r_path)
            return r_id
            #repo_options.add(r_id)

    # TODO add code to user-select repo, just in case we run into a situation
    # where we get multiple hits

    return sel_id


def validate_repo(repo):
    """Fetches the list of repositories known to the review-board server setup
    in settings.client. Checks to make sure that at least one of the current
    local remotes points to a known repository. Selects the first valid repo as
    the 'validated repository'.
    repo : pygit2.repository object for the local repository
    
    Returns: The repository information, as a dictionary, for the validated repo
    """
    # build a list of NI repository names from the configured remotes
    repo_names = set()
    for remote in repo.remotes:
        match = RE_NI_GIT.search(remote.url)
        if match:
            repo_names.add(match.group(1))
            writeout(CHAN.VERBOSE, "%s\t%s -> %s\n", remote.name, remote.url,
                     match.group(1))

    # error out if we didn't find any configured NI repositories
    if len(repo_names) is 0:
        writeout(CHAN.ERROR, "No repository names found in remotes. Either no "
                 "remotes are configured, or none of them are git.natinst.com "
                 "repos.\n")
        return False

    #writeout(CHAN.NORMAL, "=======================================\n")
    # query the review-board server for the list of valid repositories
    writeout(CHAN.NORMAL, "Querying server for repositories...")

    rb_repos = settings.client.get_root().get_repositories()
    #resp = review_board_request("repositories/")

    rb_names = set()
    for rb_repo in rb_repos:
        rb_names.add(rb_repo['name'])

    writeout(CHAN.NORMAL, "[%d repos]\n", len(rb_repos))
    #writeout(CHAN.NORMAL, "---------------------------------------\n")
    writeout(CHAN.NORMAL, "Searching for: %s\n", repo_names)
    selection = repo_names.intersection(rb_names)

    r_id = pick_repo(selection, rb_repos)
    for rb_repo in rb_repos:
        if rb_repo['id'] is r_id:
            settings.rb_repo = rb_repo
            return True
    return False

def writeout(channel, format_string, *args):
    """Conditionally prints output strings based on the configured verbosity
    (from settings.verbosity)
    
    Returns: True, if the string should have been printed to console; False,
             otherwise
    """
    if channel == CHAN.ERROR:
        sys.stderr.write("[!ERROR!] ")
        sys.stderr.write(format_string % args)
        return True

    if channel == CHAN.VERBOSE and not settings.verbose:
        return False
    else:
        sys.stdout.write(format_string % args)
        return True

if __name__ == "__main__":
    """Command line entry point"""
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("-v", "--verbose", action='store_true')
    arg_parser.add_argument("-n", "--dry-run", action='store_true')

    subparsers = arg_parser.add_subparsers()
    parser_post = subparsers.add_parser('upload')
    parser_post.set_defaults(func=command_upload)
    parser_post.add_argument('commits', nargs='?', action='store')

    parser_update = subparsers.add_parser('update')
    parser_update.set_defaults(func=command_update)

    args = arg_parser.parse_args()

    sys.exit(main(args))
