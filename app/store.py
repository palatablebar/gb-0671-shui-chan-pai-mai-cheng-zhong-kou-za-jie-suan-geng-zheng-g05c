import sqlite3, json
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("""CREATE TABLE IF NOT EXISTS seals (
        run_id TEXT PRIMARY KEY,
        seal TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        sealed_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS carryovers (
        base_run_id TEXT NOT NULL,
        current_run_id TEXT NOT NULL,
        base_seal TEXT NOT NULL,
        current_seal TEXT NOT NULL,
        draft TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT '待处理',
        published INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        published_at TEXT,
        PRIMARY KEY (base_run_id, current_run_id)
    )""")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

# ---- 封账：不可变快照，重复封存同一批次不新增记录 ----
def seal_run(path, run_id):
    result=get(path,run_id)
    if not result.get('archive'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成归档')
    seal=result['archive']['seal']
    snapshot=json.dumps({'final':result['final'],'balance':result['balance'],'archive':result['archive'],
                         'version_audit':result['version_audit'],'version_conflicts':result.get('version_conflicts',[]),
                         'manifest':result['manifest'],'quarantine':result['quarantine']},ensure_ascii=False)
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO seals(run_id,seal,snapshot) VALUES(?,?,?)',(run_id,seal,snapshot))
        row=con.execute('SELECT run_id,seal,sealed_at FROM seals WHERE run_id=?',(run_id,)).fetchone()
    return dict(row)

def get_seal(path, run_id):
    with connect(path) as con:
        row=con.execute('SELECT * FROM seals WHERE run_id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('SEAL_NOT_FOUND','封账不存在，请先在归档页封存',404)
    data=dict(row)
    data['snapshot']=json.loads(row['snapshot'])
    return data

def list_seals(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT run_id,seal,sealed_at FROM seals ORDER BY sealed_at DESC')]

# ---- 跨日承接：同一对批号只保留一条；发布事务化，失败回滚 ----
def upsert_carryover(path, draft):
    key=(draft['base_run_id'],draft['current_run_id'])
    with connect(path) as con:
        existing=con.execute('SELECT published,status FROM carryovers WHERE base_run_id=? AND current_run_id=?',key).fetchone()
        if existing and existing['published']:
            # 已发布结果不可被重跑覆盖，直接返回旧版，保证次日重跑不新增、不改写。
            row=con.execute('SELECT * FROM carryovers WHERE base_run_id=? AND current_run_id=?',key).fetchone()
            return _carryover_dict(row), False
        con.execute("""INSERT INTO carryovers(base_run_id,current_run_id,base_seal,current_seal,draft,status)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(base_run_id,current_run_id) DO UPDATE SET
                base_seal=excluded.base_seal,
                current_seal=excluded.current_seal,
                draft=excluded.draft,
                status=excluded.status,
                published=0,
                published_at=NULL""",
            (draft['base_run_id'],draft['current_run_id'],draft['base_seal'],draft['current_seal'],
             json.dumps(draft,ensure_ascii=False),draft['status']))
        row=con.execute('SELECT * FROM carryovers WHERE base_run_id=? AND current_run_id=?',key).fetchone()
    return _carryover_dict(row), True

def _carryover_dict(row):
    data=dict(row)
    data['draft']=json.loads(row['draft'])
    return data

def get_carryover(path, base_run_id, current_run_id):
    with connect(path) as con:
        row=con.execute('SELECT * FROM carryovers WHERE base_run_id=? AND current_run_id=?',(base_run_id,current_run_id)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','承接记录不存在',404)
    return _carryover_dict(row)

def list_carryovers(path):
    with connect(path) as con:
        rows=con.execute('SELECT base_run_id,current_run_id,base_seal,current_seal,status,published,created_at,published_at FROM carryovers ORDER BY rowid DESC').fetchall()
    return [dict(r) for r in rows]

def publish_carryover(path, base_run_id, current_run_id, fail_point=None):
    """发布承接结果。仅闭合草稿可发布；任何异常整体回滚，旧版封账与证据保持不变。"""
    with connect(path) as con:
        row=con.execute('SELECT * FROM carryovers WHERE base_run_id=? AND current_run_id=?',(base_run_id,current_run_id)).fetchone()
        if not row: raise BusinessError('NOT_FOUND','承接记录不存在',404)
        if row['published']:
            return _carryover_dict(row), False
        draft=json.loads(row['draft'])
        if draft['status']!='可承接' or not draft['closed'] or draft['blocked_count']:
            raise BusinessError('CARRYOVER_BLOCKED','承接差额未闭合，保留待处理，不能发布')
        if fail_point == 'before_commit':
            # 测试钩子：模拟发布写入失败，验证事务回滚。
            raise BusinessError('CARRYOVER_PUBLISH_FAILED','承接发布失败，已回滚',500)
        cur=con.execute("UPDATE carryovers SET published=1,status='已发布',published_at=datetime('now') WHERE base_run_id=? AND current_run_id=? AND published=0",(base_run_id,current_run_id))
        if cur.rowcount!=1:
            raise BusinessError('CARRYOVER_PUBLISH_FAILED','承接发布失败，并发状态变化',409)
        saved=con.execute('SELECT * FROM carryovers WHERE base_run_id=? AND current_run_id=?',(base_run_id,current_run_id)).fetchone()
    return _carryover_dict(saved), True
