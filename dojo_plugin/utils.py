import contextlib
import functools
import tempfile
import datetime
import logging
import pathlib
import tarfile
import hashlib
import inspect
import socket
import fcntl
import pytz
import os
import re

import docker
from flask import current_app, Response, abort
from itsdangerous.url_safe import URLSafeSerializer
from CTFd.models import db, Solves, Challenges, Users
from CTFd.utils.user import get_current_user
from CTFd.utils.modes import get_model
from CTFd.utils.helpers import markup
from CTFd.utils.config.pages import build_markdown
from CTFd.utils.security.sanitize import sanitize_html
from sqlalchemy import String, Integer
from sqlalchemy.sql import or_


CHALLENGES_DIR = pathlib.Path("/var/challenges")
DOJOS_DIR = pathlib.Path("/var/dojos")
HOST_DOJOS_DIR = pathlib.Path("/opt/pwn.college/data/dojos")
DOJOS_PUB_KEY = "/var/data/ssh_host_keys/ssh_host_ed25519_key.pub"
HOST_DOJOS_PRIV_KEY = "/opt/pwn.college/data/ssh_host_keys/ssh_host_ed25519_key"
PLUGIN_DIR = pathlib.Path(__file__).parent
SECCOMP = (PLUGIN_DIR / "seccomp.json").read_text()
USER_FIREWALL_ALLOWED = {
    host: socket.gethostbyname(host)
    for host in pathlib.Path("/var/user_firewall.allowed").read_text().split()
}

ID_REGEX = "^[A-Za-z0-9_.-]+$"
def id_regex(s):
    return re.match(ID_REGEX, s) and ".." not in s


def get_current_challenge_id():
    try:
        return int(current_challenge_getenv("CHALLENGE_ID"))
    except ValueError:
        return None

def get_current_dojo_challenge_id():
    try:
        return current_challenge_getenv("DOJO_CHALLENGE_ID")
    except ValueError:
        return None

def current_challenge_getenv(k):
    user = get_current_user()
    if not user:
        return None

    docker_client = docker.from_env()
    container_name = f"user_{user.id}"

    try:
        container = docker_client.containers.get(container_name)
    except docker.errors.NotFound:
        return None

    for env in container.attrs["Config"]["Env"]:
        if env.startswith(k+"="):
            return env[len(k+"=") :]
    return None

def get_active_users(active_desktops=False):
    docker_client = docker.from_env()
    containers = docker_client.containers.list(filters=dict(name="user_"), ignore_removed=True)

    def used_desktop(c):
        if c.status != 'running':
            return False

        try:
            return b"accepted" in next(c.get_archive("/tmp/vnc/vncserver.log")[0])
        except StopIteration:
            return False
        except docker.errors.NotFound:
            return False

    if active_desktops:
        containers = [ c for c in containers if used_desktop(c) ]
    uids = [ c.name.split("_")[-1] for c in containers ]
    users = [ Users.query.filter_by(id=uid).first() for uid in uids ]
    return users

def serialize_user_flag(account_id, challenge_id, *, secret=None):
    if secret is None:
        secret = current_app.config["SECRET_KEY"]
    serializer = URLSafeSerializer(secret)
    data = [account_id, challenge_id]
    user_flag = serializer.dumps(data)[::-1]
    return user_flag

def redirect_internal(redirect_uri):
    response = Response()
    response.headers["X-Accel-Redirect"] = "/internal/"
    response.headers["redirect_uri"] = redirect_uri
    return response

def redirect_user_socket(user, socket_path, url_path):
    assert user is not None
    return redirect_internal(f"http://unix:/var/homes/nosuid/{random_home_path(user)}/{socket_path}:{url_path}")

def render_markdown(s):
    return markup(build_markdown(s))

def unserialize_user_flag(user_flag, *, secret=None):
    if secret is None:
        secret = current_app.config["SECRET_KEY"]
    user_flag = re.sub(".+?{(.+)}", r"\1", user_flag)[::-1]
    serializer = URLSafeSerializer(secret)
    account_id, challenge_id = serializer.loads(user_flag)
    return account_id, challenge_id


def simple_tar(path, name=None):
    f = tempfile.NamedTemporaryFile()
    t = tarfile.open(mode="w", fileobj=f)
    abs_path = os.path.abspath(path)
    t.add(abs_path, arcname=(name or os.path.basename(path)))
    t.close()
    f.seek(0)
    return f


def random_home_path(user, *, secret=None):
    if secret is None:
        secret = current_app.config["SECRET_KEY"]
    return hashlib.sha256(f"{secret}_{user.id}".encode()).hexdigest()[:16]

def dojo_by_id(dojo_id):
    dojo = Dojos.query.filter_by(id=dojo_id).first()
    if not dojo:
        return None
    if dojo.public:
        return dojo

    user = get_current_user()
    if not user:
        return None
    if user.id == dojo.owner_id:
        return dojo
    if not DojoMembers.query.filter_by(dojo_id=dojo.id, user_id=user.id).first():
        return None
    return dojo


def module_visible(dojo, module, user):
    return (
        "time_visible" not in module or
        module["time_visible"] <= datetime.datetime.now(pytz.utc) or
        is_dojo_admin(user, dojo)
    )


def module_challenges_visible(dojo, module, user):
    return (
        "time_assigned" not in module or
        module["time_assigned"] <= datetime.datetime.now(pytz.utc) or
        is_dojo_admin(user, dojo)
    )


def is_dojo_admin(user, dojo):
    return user and dojo and (user.type == "admin" or dojo.owner_id == user.id)


def dojo_route(func):
    signature = inspect.signature(func)
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        bound_args = signature.bind(*args, **kwargs)
        bound_args.apply_defaults()
        dojo = bound_args.arguments["dojo"]
        if dojo is not None:
            dojo = dojo_by_id(dojo)
            if not dojo:
                abort(404)
        bound_args.arguments["dojo"] = dojo

        with contextlib.suppress(KeyError):
            module = bound_args.arguments["module"]
            if module is not None:
                module = dojo.module_by_id(module)
                if not module or not module_visible(dojo, module, get_current_user()):
                    abort(404)

            bound_args.arguments["module"] = module

        return func(*bound_args.args, **bound_args.kwargs)
    return wrapper


def user_dojos(user):
    filters = [Dojos.public == True]
    if user:
        filters.append(Dojos.owner_id == user.id)
        members = db.session.query(DojoMembers.dojo_id).filter(DojoMembers.user_id == user.id)
        filters.append(Dojos.id.in_(members.subquery()))
    return Dojos.query.filter(or_(*filters)).all()


def dojo_standings(dojo_id=None, fields=None, module_id=None):
    if fields is None:
        fields = []

    Model = get_model()

    dojo_filters = []
    if dojo_id is None:
        dojos = Dojos.query.filter_by(public=True).all()
        dojo_filters.append(or_(*(dojo.challenges_query(module_id=module_id) for dojo in dojos)))
    else:
        dojo = Dojos.query.filter(Dojos.id == dojo_id).first()
        dojo_filters.append(dojo.challenges_query(module_id=module_id))

        if not dojo.public:
            members = db.session.query(DojoMembers.user_id).filter_by(dojo_id=dojo_id)
            dojo_filters.append(Solves.account_id.in_(members.subquery()))

    standings_query = (
        db.session.query(*fields)
        .join(Challenges)
        .join(Model, Model.id == Solves.account_id)
        .filter(Challenges.value != 0, Model.banned == False, Model.hidden == False,
                *dojo_filters)
    )

    return standings_query


def load_dojo(dojo_id, dojo_spec, user=None, dojo_dir=None, commit=True, log=logging.getLogger("__name__"), initial_join_code=None):
    log.info("Initiating dojo load.")

    dojo = Dojos.query.filter_by(id=dojo_id).first()
    if not dojo:
        dojo = Dojos(id=dojo_id, owner_id=None if not user else user.id, data=dojo_spec)
        dojo.join_code = initial_join_code
        log.info("Dojo is new, adding.")
        db.session.add(dojo)
    elif dojo.data == dojo_spec:
        # make sure the previous load was fully successful (e.g., all the imports worked and weren't fixed since then)
        num_loaded_chals = DojoChallenges.query.filter_by(dojo_id=dojo_id).count()
        num_spec_chals = sum(len(module.get("challenges", [])) for module in dojo.config.get("modules", []))

        if num_loaded_chals == num_spec_chals:
            log.warning("Dojo is unchanged, aborting update.")
            return
    else:
        dojo.data = dojo_spec
        db.session.add(dojo)

    if dojo.config.get("dojo_spec", None) != "v2":
        log.warning("Incorrect dojo spec version (dojo_spec attribute). Should be 'v2'")

    dojo.apply_spec(dojo_log=log, dojo_dir=dojo_dir)

    if commit:
        log.info("Committing database changes!")
        db.session.commit()
    else:
        log.info("Rolling back database changes!")
        db.session.rollback()


# this is a MASSIVE hack and should be replaced with something less insane
_lock_number = [ 0 ]
def multiprocess_lock(func):
    _lock_filename = f"/dev/shm/dojolock-{_lock_number[0]}"
    _lock_number[0] += 1
    open(_lock_filename, "w").close()
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        lf = open(_lock_filename, "r")
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return func(*args, **kwargs)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    return wrapper

def dojo_completions():
    all_solves = (
        db.session.query(DojoChallenges.dojo_id.label("dojo_id"))
        .join(Solves, DojoChallenges.challenge_id == Solves.challenge_id)
        .add_columns(
            db.func.count(Solves.id).label("solves"),
            Solves.user_id,
            db.func.max(Solves.date).label("last_solve"),
            db.func.min(Solves.date).label("first_solve"),
        )
        .group_by(Solves.user_id, DojoChallenges.dojo_id)
        .order_by("last_solve")
    ).all()
    all_challenges = (
        db.session.query(Dojos.id.label("dojo_id"))
        .join(DojoChallenges, DojoChallenges.dojo_id == Dojos.id)
        .add_columns(db.func.count(DojoChallenges.challenge_id).label("challenges"))
        .group_by(Dojos.id)
    ).all()

    chal_counts = { d.dojo_id: d.challenges for d in all_challenges }
    completions = { }
    for s in all_solves:
        if s.solves == chal_counts[s.dojo_id]:
            completions.setdefault(s.user_id, []).append({
                "dojo": s.dojo_id, "last_solve": s.last_solve, "first_solve": s.first_solve
            })
    return completions

def first_bloods():
    first_blood_string = db.func.min(Solves.date.cast(String)+"|"+Solves.user_id.cast(String))
    first_blood_query = (
        db.session.query(Challenges.id.label("challenge_id"))
        .join(Solves, Challenges.id == Solves.challenge_id)
        .add_columns(
            db.func.substring_index(first_blood_string, "|", -1).cast(Integer).label("user_id"),
            db.func.min(Solves.date).label("timestamp")
        )
        .group_by(Challenges.id)
        .order_by("timestamp")
    ).all()
    return first_blood_query

def daily_solve_counts():
    counts = (
        db.session.query(
            Solves.user_id, db.func.count(Solves.challenge_id).label("solves"),
            db.func.year(Solves.date).label("year"),
            db.func.month(Solves.date).label("month"),
            db.func.day(Solves.date).label("day")
        )
        .join(Challenges, Challenges.id == Solves.challenge_id)
        .filter(~Challenges.category.contains("embryo"))
        .group_by("year", "month", "day", Solves.user_id)
    ).all()
    return counts


def belt_challenges():
    # TODO: move this concept into dojo yml

    yellow_categories = [
        "embryoio",
        "babysuid",
        "embryoasm",
        "babyshell",
        "babyjail",
        "embryogdb",
        "babyrev",
        "babymem",
        "toddlerone",
    ]

    blue_categories = [
        *yellow_categories,
        "babyrop",
        "babyheap",
        "babyrace",
        "babykernel",
        "toddlertwo",
    ]

    color_categories = {
        "yellow": yellow_categories,
        "blue": blue_categories,
    }

    return {
        color: db.session.query(Challenges.id).filter(
            Challenges.state == "visible",
            Challenges.value > 0,
            Challenges.id < 1000,
            Challenges.category.in_(categories),
        )
        for color, categories in color_categories.items()
    }

# based on https://stackoverflow.com/questions/36408496/python-logging-handler-to-append-to-list
class ListHandler(logging.Handler): # Inherit from logging.Handler
    def __init__(self, log_list):
        logging.Handler.__init__(self)
        self.log_list = log_list

    def emit(self, record):
        self.log_list.append(record.levelname + ": " + record.getMessage())

class HTMLHandler(logging.Handler): # Inherit from logging.Handler
    def __init__(self, start_tag="<code>", end_tag="</code>", join_tag="<br>"):
        logging.Handler.__init__(self)
        self.html = ""
        self.start_tag = start_tag
        self.end_tag = end_tag
        self.join_tag = join_tag

    def reset(self):
        self.html = ""

    def emit(self, record):
        if self.html:
            self.html += self.join_tag
        self.html += f"{self.start_tag}<b>{record.levelname}</b>: {sanitize_html(record.getMessage())}{self.end_tag}"

def ctfd_to_host_path(ctfd_path):
    """
    Converts a path inside the CTFd docker to a path outside the docker.
    Unfortunately, very infra-dependent.
    """

    with contextlib.suppress(ValueError):
        return HOST_DOJOS_DIR/ctfd_path.relative_to(DOJOS_DIR)

def sandboxed_git_clone(dojo_repo, repo_dir):
    return sandboxed_git_command(
        repo_dir,
        [ "clone", "--quiet", "--depth", "1", dojo_repo, "/tmp/repo_dir" ]
    )

def sandboxed_git_command(repo_dir, command):
    docker_client = docker.from_env()
    container = docker_client.containers.run(
        "alpine/git", [ "-C", "/tmp/repo_dir" ] + command,
        environment={
            "GIT_SSH_COMMAND":
            f"ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR -i /tmp/deploykey"
        },
        mounts=[
            docker.types.Mount( "/tmp/repo_dir", repo_dir, "bind", propagation="shared"),
            docker.types.Mount("/tmp/deploykey", HOST_DOJOS_PRIV_KEY, "bind")
        ],
        detach=True,
        cpu_quota=100000, mem_limit="1000m",
        stdout=True, stderr=True
    )
    returncode = container.wait()['StatusCode']
    output = container.logs()
    container.remove()

    return returncode, output

from .models import Dojos, DojoMembers, DojoChallenges
