"""Consent-based household invitations and lossless household-state migration."""
import json
import sqlite3

def schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS hg_join_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_id INTEGER NOT NULL REFERENCES hg_households(id),
        user_id INTEGER NOT NULL REFERENCES hg_users(id),
        invited_by INTEGER NOT NULL REFERENCES hg_users(id),
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','declined')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS hg_household_merge_backups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL,
        household_id INTEGER NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")

def details(conn, uid, hid):
    schema(conn)
    house=conn.execute("SELECT name FROM hg_households WHERE id=?",(hid,)).fetchone()
    role=conn.execute("SELECT role FROM hg_memberships WHERE user_id=? AND household_id=?",(uid,hid)).fetchone()
    members=conn.execute("SELECT u.email,m.role FROM hg_memberships m JOIN hg_users u ON u.id=m.user_id WHERE m.household_id=? ORDER BY u.email",(hid,)).fetchall()
    pending=conn.execute("""SELECT r.id,h.name FROM hg_join_requests r JOIN hg_households h ON h.id=r.household_id
        WHERE r.user_id=? AND r.status='pending' ORDER BY r.id DESC""",(uid,)).fetchall()
    return {"name":house[0],"role":role[0] if role else "member","members":[{"email":e,"role":r} for e,r in members],
            "pending":[{"id":i,"name":n} for i,n in pending]}

def invite(conn, uid, hid, email):
    schema(conn)
    role=conn.execute("SELECT role FROM hg_memberships WHERE user_id=? AND household_id=?",(uid,hid)).fetchone()
    if not role or role[0]!="owner":raise PermissionError("Only household owners can invite")
    target=conn.execute("SELECT id FROM hg_users WHERE email=? COLLATE NOCASE",(email,)).fetchone()
    if not target:raise ValueError("No account with that email. Invite them to register first.")
    if target[0]==uid or conn.execute("SELECT 1 FROM hg_memberships WHERE user_id=? AND household_id=?",(target[0],hid)).fetchone():
        raise ValueError("Already a member of this household")
    if conn.execute("SELECT 1 FROM hg_join_requests WHERE user_id=? AND household_id=? AND status='pending'",(target[0],hid)).fetchone():
        raise ValueError("An invitation is already pending")
    conn.execute("INSERT INTO hg_join_requests(household_id,user_id,invited_by) VALUES(?,?,?)",(hid,target[0],uid))

def merge_state(destination,source):
    """Preserve all distinct data; reject ambiguous collisions rather than overwrite."""
    if not isinstance(destination,dict) or not isinstance(source,dict):raise ValueError("Invalid household state")
    out=json.loads(json.dumps(destination))
    for key,value in source.items():
        if key=="revision":continue
        if key not in out or out[key] in (None,{},[],""):
            out[key]=value
        elif value in (None,{},[],"") or out[key]==value:
            continue
        elif isinstance(out[key],dict) and isinstance(value,dict):
            for subkey,subvalue in value.items():
                if subkey not in out[key]:out[key][subkey]=subvalue
                elif out[key][subkey]!=subvalue:
                    raise ValueError("Merge conflict in "+key+": "+str(subkey)+". Resolve before joining.")
        elif key=="list" and isinstance(out[key],list) and isinstance(value,list):
            # Shopping items may share a name with different quantities/status.
            # Retain every distinct entry rather than silently overwrite it.
            for item in value:
                if item not in out[key]:out[key].append(item)
        elif isinstance(out[key],list) and isinstance(value,list):
            for item in value:
                if item not in out[key]:out[key].append(item)
        else:
            raise ValueError("Merge conflict in "+key+". Resolve before joining.")
    out["revision"]=max(int(destination.get("revision",0)),int(source.get("revision",0)))+1
    return out

def respond(conn, uid, request_id, accept):
    schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row=conn.execute("SELECT household_id FROM hg_join_requests WHERE id=? AND user_id=? AND status='pending'",(request_id,uid)).fetchone()
        if not row:raise ValueError("Invitation unavailable")
        if accept:
            target=row[0]
            memberships=conn.execute("SELECT household_id,role FROM hg_memberships WHERE user_id=?",(uid,)).fetchall()
            if len(memberships)!=1:raise ValueError("Contact the administrator: multiple households need review")
            old,role=memberships[0]
            if old!=target:
                if conn.execute("SELECT COUNT(*) FROM hg_memberships WHERE household_id=?",(old,)).fetchone()[0]>1:
                    raise ValueError("Your current household has other members. Its data cannot be merged without their consent")
                source=conn.execute("SELECT payload FROM hg_household_state WHERE household_id=?",(old,)).fetchone()
                destination=conn.execute("SELECT payload FROM hg_household_state WHERE household_id=?",(target,)).fetchone()
                if not source or not destination:raise ValueError("Household data missing; no changes made")
                merged=merge_state(json.loads(destination[0]),json.loads(source[0]))
                for household_id,payload in ((old,source[0]),(target,destination[0])):
                    conn.execute("INSERT INTO hg_household_merge_backups(request_id,household_id,payload) VALUES(?,?,?)",(request_id,household_id,payload))
                conn.execute("UPDATE hg_household_state SET payload=? WHERE household_id=?",(json.dumps(merged),target))
                conn.execute("DELETE FROM hg_memberships WHERE user_id=? AND household_id=?",(uid,old))
                conn.execute("INSERT OR IGNORE INTO hg_memberships(user_id,household_id,role) VALUES(?,?,'member')",(uid,target))
        conn.execute("UPDATE hg_join_requests SET status=? WHERE id=?",("accepted" if accept else "declined",request_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
