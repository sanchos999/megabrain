"""M5.3E resolver safety contract tests."""
from projects.resolver import ProjectResolver


class PG:
    def __init__(self):
        self.projects = {"X": {"project_id":"X","name":"Project X"}, "Y": {"project_id":"Y","name":"Project Y"}}
        self.sessions = {}
        self.profile = {}
    def get_project(self, pid): return self.projects.get(pid)
    def get_session_project(self, sid): return self.sessions.get(sid)
    def bind_project_context(self, **kw):
        self.sessions[kw["session_id"]] = kw["project_id"]
        self.profile[(kw["source"],kw["profile"],kw["channel"])] = kw["project_id"]
    def get_profile_project(self, **kw): return self.profile.get((kw["source"],kw["profile"],kw["channel"]))
    def list_projects(self): return list(self.projects.values())

class Redis:
    def get_str(self, key): return None

class Telemetry:
    def observe(self,*a): pass
    def inc(self,*a): pass

def resolver():
    pg=PG(); return ProjectResolver(pg, None, Redis(), Telemetry()), pg

def test_resume_inherits_parent_over_foreign_recent():
    r,pg=resolver(); pg.sessions["parent"]="X"; pg.profile[("hermes","r7canary","cli") ]="Y"
    got=r.resolve(session_id="child", parent_session_id="parent", source="hermes", profile="r7canary", channel="cli")
    assert got["project_id"]=="X" and got["reason"]=="RESUME_INHERIT"

def test_existing_session_is_authoritative():
    r,pg=resolver(); pg.sessions["s"]="X"
    got=r.resolve(session_id="s", project_id="Y")
    assert got["project_id"]=="Y" and got["reason"]=="EXPLICIT"

def test_profile_active_is_scoped():
    r,pg=resolver(); pg.profile[("hermes","r7canary","cli") ]="X"; pg.profile[("hermes","default","cli") ]="Y"
    assert r.resolve(query="продолжаем",source="hermes",profile="r7canary",channel="cli")["project_id"]=="X"
    assert r.resolve(query="продолжаем",source="hermes",profile="default",channel="cli")["project_id"]=="Y"

def test_unresolved_does_not_choose_global_recent():
    r,pg=resolver(); got=r.resolve(query="продолжаем",source="hermes",profile="new",channel="cli")
    assert got["project_id"] is None and got["reason"]=="UNRESOLVED"

def test_ambiguous_signal_is_safe():
    r,pg=resolver(); pg.projects["Y"]["name"]="Project X"
    got=r.resolve(query="project x")
    assert got["project_id"] is None and got["reason"]=="AMBIGUOUS"

def test_reproduction_before_after():
    r,pg=resolver(); pg.sessions["first"]="X"; pg.profile[("hermes","r7canary","cli") ]="Y"
    assert r.resolve(session_id="resume", query="продолжаем", source="hermes", profile="new", channel="cli")["project_id"] is None
    assert r.resolve(session_id="resume2", parent_session_id="first", query="продолжаем", source="hermes", profile="r7canary", channel="cli")["project_id"]=="X"
