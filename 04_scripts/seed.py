"""初期データを raw に登録する（開発手順書 Step 3、要件定義書1-3・10-4）。

登録するのは**出典のある全国共通の支援制度の情報だけ**（組織のIDが空の行。追加修正②）。
組織と参加コードは入れない。組織はアプリの「組織を作る」で作る（要望2）。
2026-11-10 までは初期データとして輪島市・珠洲市の2組織と参加コードを入れていたが、
アプリで組織を作れるようになり、全国共通の制度で組織がない本人にも支援を出せるため、やめた。
被災者・施設・記録は、デモでも画面から入力する（要件定義書1-3）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/seed.py            # 全国共通の制度を入れる
    python 04_scripts/seed.py --dry-run  # 流さずに、何を登録するかだけ確かめる

守ること:

- 何度流しても同じ状態になる。すでに同じ内容が入っているものは**登録せずに飛ばす**
  （raw は追記だけで消せないため、同じ行を増やさない）。内容を直したときだけ新しい版を入れる。
- 支援制度は**出典のある公的な情報だけ**（要件定義書11-3「制度の情報にない制度をAIに作らせない」の前提）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))

from backend.config import get_settings  # noqa: E402
from backend.services import bq  # noqa: E402

# 初期設定の行を入れた人。表の説明のとおり、人ではなく仕組みが入れたことを表す。
SYSTEM_USER = "system"
SYSTEM_ROLE = "system"

# 支援制度の出典（要件定義書14章）。
# ［4］内閣府「被災者の住まいの確保等に関する枠組み」p.2 … 支給額と対象の条件
SOURCE_URL_SUMAI = "https://www.bousai.go.jp/taisaku/pdf/sumai/sumai_1.pdf"
# ［17］山梨県「被災者生活再建支援制度について」 … 申請期間・申請窓口・必要書類（［4］に記載がない）
SOURCE_URL_SHINSEI = "https://www.pref.yamanashi.jp/bousai/hisaisyaseikatsusaikenshien.html"

# `source_url` に入れる出典。1つの列に複数のURLを空白区切りで入れ、
# **行に書いたすべての内容がどれかのURLで裏づけられる**状態にする（要件定義書11-3）。
# F6 の画面では空白で分けてリンクにする。
SOURCE_URLS = f"{SOURCE_URL_SUMAI} {SOURCE_URL_SHINSEI}"

# ［18］内閣府「被災者支援に関する各種制度の概要」（令和8年6月1日現在） … 使いやすさの改修の計画.md 6-2
# で足した制度（災害弔慰金・災害障害見舞金・災害援護資金・生活福祉資金・緊急の修理・応急修理）の出典。
# 制度の名称・内容・活用できる方・問い合わせ先は、この資料の p.1・p.3・p.4・p.29 の文言に合わせた。
# 資料に申請の期限と必要な書類の記載がない制度は、そう書いて窓口で確かめるよう案内する（作らない）。
SOURCE_URL_KAKUSHU = "https://www.bousai.go.jp/taisaku/hisaisyagyousei/pdf/kakusyuseido_tsuujou.pdf"

# 出典に申請の期限・必要な書類が書かれていないときの文言（制度ごとに書き写さない）。
NO_DEADLINE_IN_SOURCE = "出典の資料に申請の期限の記載はない。申し込み先の窓口で確かめる。"
NO_DOCUMENTS_IN_SOURCE = "出典の資料に必要な書類の記載はない。申し込み先の窓口で確かめる。"


@dataclass(frozen=True)
class SeedProgram:
    """初期設定で登録する支援制度。出典のある公的な情報だけを持つ。"""

    key: str
    program_name: str
    conditions: str
    benefit: str
    apply_to: str
    deadline_rule: str
    documents: str
    source_url: str


# 支援制度。金額と対象の条件は出典［4］（内閣府「被災者の住まいの確保等に関する枠組み」p.2）。
# 申請先・申請の期限・必要な書類は［4］に書かれていないため、要件定義書14章に出典［17］を
# 足してそれに合わせた（開発ログの要確認9）。どの行も、`source_url` のどれかのURLで裏が取れる。
SEED_PROGRAMS: tuple[SeedProgram, ...] = (
    SeedProgram(
        key="seikatsu_saiken_kiso",
        program_name="被災者生活再建支援金（基礎支援金）",
        conditions=(
            "被災者生活再建支援法が適用された市町村の世帯のうち、"
            "住宅が全壊した世帯／住宅が半壊または住宅の敷地に被害が生じ、やむを得ず解体した世帯／"
            "災害による危険な状況が続き、長期間自宅に住めない世帯／"
            "住宅が半壊し、大規模な補修をしなければ住むことが難しい世帯（大規模半壊）。"
            "資力要件はない。"
        ),
        benefit=(
            "全壊・解体・長期避難の世帯は100万円、大規模半壊の世帯は50万円。"
            "いずれも複数人の世帯の額で、1人の世帯は4分の3の額になる。"
        ),
        apply_to="被災した当時に住んでいた市町村の窓口",
        deadline_rule=(
            "災害が発生した日から13か月以内。"
            "世帯主がやむを得ない事情で申請できないときは、期間が延長されることがある。"
        ),
        documents=(
            "支給申請書、罹災証明書、世帯全員分の住民票、世帯主名義の預金通帳の写しなど。"
            "解体した場合は解体証明書または滅失登記簿謄本も要る。"
            "必要な書類は申請先の市町村の案内で確かめる。"
        ),
        source_url=SOURCE_URLS,
    ),
    SeedProgram(
        key="seikatsu_saiken_kasan",
        program_name="被災者生活再建支援金（加算支援金）",
        conditions=(
            "基礎支援金の対象になる世帯が、住宅を建設・購入したとき、補修したとき、"
            "または賃借したとき。資力要件はない。"
            "補修は、借家は原則として対象にならず、半壊してやむを得ず解体した場合も除く。"
            "賃借は公営住宅を除く。"
        ),
        benefit=(
            "建設・購入は200万円、補修は100万円、賃借（公営住宅を除く）は50万円。"
            "いずれも複数人の世帯の額で、1人の世帯は4分の3の額になる。"
        ),
        apply_to="被災した当時に住んでいた市町村の窓口",
        deadline_rule=(
            "災害が発生した日から37か月以内。"
            "世帯主がやむを得ない事情で申請できないときは、期間が延長されることがある。"
        ),
        documents=(
            "支給申請書と、住宅の建設・購入・補修・賃借の契約書など。"
            "必要な書類は申請先の市町村の案内で確かめる。"
        ),
        source_url=SOURCE_URLS,
    ),
    # --- ここから［18］内閣府「被災者支援に関する各種制度の概要」（令和8年6月1日現在） ---
    # 使いやすさの改修の計画.md 6-2。資料の文言を、意味を変えずに短くした。
    SeedProgram(
        key="saigai_choui_kin",
        program_name="災害弔慰金",
        conditions=(
            "災害により死亡した方のご遺族（配偶者、子、父母、孫、祖父母。いずれもいない場合は、"
            "死亡当時その方と同居し、または生計を同じくしていた兄弟姉妹）。"
            "対象となる災害は、自然災害で1市町村において住居が5世帯以上滅失した災害等。"
        ),
        benefit=(
            "給付。生計維持者が死亡した場合は市町村条例で定める額（500万円以下）、"
            "その他の者が死亡した場合は市町村条例で定める額（250万円以下）。"
        ),
        apply_to="市町村",
        deadline_rule=NO_DEADLINE_IN_SOURCE,
        documents=NO_DOCUMENTS_IN_SOURCE,
        source_url=SOURCE_URL_KAKUSHU,
    ),
    SeedProgram(
        key="saigai_shougai_mimai_kin",
        program_name="災害障害見舞金",
        conditions=(
            "災害による負傷・疾病で、精神または身体に重い障害を受けた方"
            "（両眼が失明した人、常に介護を要する人など、出典の資料に挙げられた程度の障害）。"
            "対象となる災害は、自然災害で1市町村において住居が5世帯以上滅失した災害等。"
        ),
        benefit=(
            "給付。生計維持者が重度の障害を受けた場合は市町村条例で定める額（250万円以下）、"
            "その他の者が重度の障害を受けた場合は市町村条例で定める額（125万円以下）。"
        ),
        apply_to="市町村",
        deadline_rule=NO_DEADLINE_IN_SOURCE,
        documents=NO_DOCUMENTS_IN_SOURCE,
        source_url=SOURCE_URL_KAKUSHU,
    ),
    SeedProgram(
        key="saigai_engo_shikin",
        program_name="災害援護資金",
        conditions=(
            "次のいずれかの被害を受けた世帯の世帯主。"
            "1. 世帯主が災害により負傷し、その療養に要する期間がおおむね1か月以上。"
            "2. 家財の3分の1以上の損害。3. 住居の半壊または全壊・流出。"
            "所得制限がある（市町村民税における前年の総所得金額が、1人世帯220万円、2人430万円、"
            "3人620万円、4人730万円、5人以上は1人増すごとに730万円に30万円を加えた額。"
            "住居が滅失した場合は1,270万円）。"
            "対象となる災害は、自然災害で都道府県において災害救助法が適用された市町村が1以上ある場合などの災害。"
        ),
        benefit=(
            "貸付（返す必要がある）。生活の再建に必要な資金を貸し付ける。"
            "貸付限度額は、被害の内容により150万円から350万円。"
            "貸付利率は年3%以内で条例で定める率（据置期間中は無利子）。"
            "据置期間は3年以内（特別の場合5年）、償還期間は10年以内（据置期間を含む）。"
        ),
        apply_to="市町村",
        deadline_rule=NO_DEADLINE_IN_SOURCE,
        documents=NO_DOCUMENTS_IN_SOURCE,
        source_url=SOURCE_URL_KAKUSHU,
    ),
    SeedProgram(
        key="seikatsu_fukushi_shikin",
        program_name="生活福祉資金制度による貸付（緊急小口資金・福祉費（災害援護費））",
        conditions=(
            "低所得世帯、障害者世帯または高齢者世帯。"
            "福祉費（災害援護費）は、災害弔慰金の支給等に関する法律の災害援護資金の対象となる世帯は適用除外。"
        ),
        benefit=(
            "貸付（返す必要がある）。"
            "緊急小口資金：貸付限度額10万円以内、無利子、据置期間は貸付けの日から2月以内、"
            "償還期間は据置期間経過後12月以内。"
            "福祉費（災害援護費）：貸付限度額150万円（目安）、連帯保証人を立てた場合は無利子・"
            "立てない場合は年1.5%、据置期間は貸付けの日から6月以内、償還期間は据置期間経過後7年以内（目安）。"
            "大規模災害時には、貸付対象世帯の拡大などの特例措置を実施することがある。"
        ),
        apply_to="都道府県社会福祉協議会または市町村社会福祉協議会",
        deadline_rule=NO_DEADLINE_IN_SOURCE,
        documents=NO_DOCUMENTS_IN_SOURCE,
        source_url=SOURCE_URL_KAKUSHU,
    ),
    SeedProgram(
        key="kinkyu_shuuri",
        program_name="住家の被害の拡大を防止するための緊急の修理（ブルーシートの展張等）",
        conditions=(
            "災害救助法に基づく。住宅の屋根、外壁、建具（窓や玄関）等に損傷があり、"
            "雨水の浸入等を放置すれば被害が拡大するおそれがある世帯で、"
            "自治体から準半壊以上（相当）と判断された方。"
            "この判断は、自治体職員による現場確認、または被害を受けた方が持参した写真で行う。"
            "住家が対象で、物置、倉庫や駐車場等は対象にならない。"
        ),
        benefit=(
            "現物給付（お金ではなく、資材や作業で受ける）。"
            "ブルーシート、ロープ、土のう等の資材の現物給付、または修理業者・団体による"
            "ブルーシート展張等のいずれか。基準額は1世帯当たり5万6,400円以内（令和8年4月）。"
        ),
        apply_to="都道府県、災害救助法が適用された市町村",
        deadline_rule=(
            "出典の資料では、緊急の修理は災害発生日から10日以内に行うとされている。"
            "申し込みの期限は、申し込み先の窓口で確かめる。"
        ),
        documents=(
            "出典の資料に必要な書類の記載はない。被害の判断に、被害を受けた方が持参した写真を"
            "使うことがある。申し込み先の窓口で確かめる。"
        ),
        source_url=SOURCE_URL_KAKUSHU,
    ),
    SeedProgram(
        key="juutaku_oukyuu_shuuri",
        program_name="日常生活に必要な最小限度の部分の修理（住宅の応急修理）",
        conditions=(
            "災害救助法が適用された市町村で、り災証明に「大規模半壊、中規模半壊、半壊（半焼）、準半壊」"
            "と記載されている方。中規模半壊・半壊（半焼）・準半壊の世帯は、自ら修理する資力がない世帯が対象。"
            "全壊（全焼）の場合は、応急修理により居住可能となる場合に限り、対象となることがある。"
        ),
        benefit=(
            "現物給付（お金ではなく、修理で受ける）。被災した住宅の居室、台所、トイレ等、"
            "日常生活に必要な最小限度の部分を、都道府県等が修理業者を通じて応急的に修理する。"
            "基準額は1世帯当たり、大規模半壊・中規模半壊・半壊（半焼）の世帯は75万7千円以内、"
            "準半壊の世帯は36万7千円以内（令和8年4月）。"
        ),
        apply_to="都道府県、災害救助法が適用された市町村",
        deadline_rule=(
            "出典の資料に申し込みの期限の記載はない。申し込み先の窓口で確かめる。"
            "応急修理の期間が1か月を超えると見込まれ、自宅が半壊（住宅としての利用ができない場合）以上の"
            "被害を受け、ほかの住まいの確保が困難な方は、応急仮設住宅に入居できる"
            "（入居期限は応急修理を申し込んだ日から原則6か月以内）。"
        ),
        documents=(
            "り災証明書（対象になる方が、り災証明の記載で決まるため）。"
            "そのほかの書類は、申し込み先の窓口で確かめる。"
        ),
        source_url=SOURCE_URL_KAKUSHU,
    ),
)

# 全国共通の制度（追加修正②）。`raw_programs` の `org_id` を空にした行で、組織に
# つながっていない本人にも出す（`backend/services/supports.py` の `list_programs`）。
# 被災者生活再建支援金は、内閣府が所管する国の制度（被災者生活再建支援法）で、
# 上の SEED_PROGRAMS を、全国共通の制度として入れる（出典［4］［17］で裏が取れるもの）。
# 組織に入っている本人には、同じ名前の組織の制度があればそちらを出す（同じ制度を2つ
# 並べない。2026-11-10 より前に入れた輪島市・珠洲市の制度など）。ほかの国の制度は、出典を確かめてから足す（ここで作らない。要件定義書11-3）。
NATIONWIDE_ORG_ID = ""
NATIONWIDE_PROGRAM_PREFIX = "nationwide"
NATIONWIDE_LABEL = "全国共通"

# 制度の内容として比べる列（ここが変わったときだけ新しい版を入れる）。
PROGRAM_CONTENT_COLUMNS = (
    "program_name",
    "conditions",
    "benefit",
    "apply_to",
    "deadline_rule",
    "documents",
    "source_url",
)


# --- 登録する行を組み立てる（BigQuery には接続しない） ---


def program_id_for(org_id: str, key: str) -> str:
    """制度のIDを作る。

    `stg_programs` は `program_id` だけで最新の版を取り出すため、組織ごとの制度
    （2026-11-10 より前に入れた輪島市・珠洲市の分など）とぶつからないよう、IDに組織を含める。
    全国共通の制度（組織のIDが空。追加修正②）は `nationwide__<key>` にする。
    """
    return f"{org_id or NATIONWIDE_PROGRAM_PREFIX}__{key}"


def build_program_row(
    org_id: str,
    program: SeedProgram,
    *,
    version: int,
    now: dt.datetime,
    event_id: str | None = None,
) -> dict[str, object]:
    """`raw_programs` に入れる1行を作る。"""
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": now,
        "created_by": SYSTEM_USER,
        "created_role": SYSTEM_ROLE,
        "program_id": program_id_for(org_id, program.key),
        "version": version,
        "program_name": program.program_name,
        "conditions": program.conditions,
        "benefit": program.benefit,
        "apply_to": program.apply_to,
        "deadline_rule": program.deadline_rule,
        "documents": program.documents,
        "source_url": program.source_url,
    }


@dataclass(frozen=True)
class PlannedRow:
    """登録する1行（または飛ばす1行）。"""

    table: str
    label: str
    row: dict[str, object] | None
    reason: str


def plan(
    *,
    existing_programs: dict[str, dict[str, object]],
    now: dt.datetime,
    programs: tuple[SeedProgram, ...] = SEED_PROGRAMS,
) -> list[PlannedRow]:
    """今の中身（`stg_programs` の行）と比べて、登録する行を決める。

    すでに同じ内容が入っているものは `row=None` にして飛ばす。
    何度流しても同じ状態になるのはここで決まる。
    """
    planned: list[PlannedRow] = []
    for program in programs:
        program_id = program_id_for(NATIONWIDE_ORG_ID, program.key)
        current = existing_programs.get(program_id)
        label = f"{program.program_name}／{NATIONWIDE_LABEL}"
        if current is None:
            row = build_program_row(NATIONWIDE_ORG_ID, program, version=1, now=now)
            planned.append(PlannedRow("raw_programs", label, row, "新しく登録する（版1）"))
            continue
        if _same(
            current,
            build_program_row(NATIONWIDE_ORG_ID, program, version=1, now=now),
            PROGRAM_CONTENT_COLUMNS,
        ):
            planned.append(PlannedRow("raw_programs", label, None, "同じ内容が入っている"))
            continue
        version = int(current.get("version") or 0) + 1
        row = build_program_row(NATIONWIDE_ORG_ID, program, version=version, now=now)
        planned.append(
            PlannedRow("raw_programs", label, row, f"内容が変わったので版{version}を入れる")
        )
    return planned


def _same(current: dict[str, object], row: dict[str, object], columns: tuple[str, ...]) -> bool:
    """比べる列だけを見て、同じ内容かを返す。"""
    return all(current.get(column) == row.get(column) for column in columns)


# --- 今の中身を読む ---


def fetch_existing_programs(settings) -> dict[str, dict[str, object]]:
    """`stg_programs` の今の中身を、制度のIDで引けるようにして返す。"""
    columns = ", ".join(("program_id", "org_id", "version") + PROGRAM_CONTENT_COLUMNS)
    sql = (
        f"SELECT {columns}\n"
        f"FROM `{settings.project_id}.{settings.dataset_staging}.stg_programs`"
    )
    return {row["program_id"]: row for row in bq.query(sql)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初期データ（全国共通の支援制度）を raw に登録する")
    parser.add_argument("--dry-run", action="store_true", help="登録せずに、何をするかだけ表示する")
    args = parser.parse_args(argv)

    settings = get_settings()
    print("=== 設定 ===")
    print(f"プロジェクト : {settings.project_id or '(未設定)'}")
    print(f"場所         : {settings.bq_location}")
    print()

    if not settings.project_id:
        print("NG: .env の GOOGLE_CLOUD_PROJECT を設定してください。")
        return 1

    try:
        existing_programs = fetch_existing_programs(settings)
    except bq.BigQueryUnavailable as exc:
        print(f"NG: BigQuery を読めません: {exc}")
        print("- 03_sql を流し終えているか（python 04_scripts/run_sql.py --all）")
        print("- 認証: gcloud auth application-default login を済ませたか")
        return 1

    planned = plan(existing_programs=existing_programs, now=dt.datetime.now(dt.timezone.utc))

    inserted = 0
    skipped = 0
    for item in planned:
        if item.row is None:
            print(f"- 飛ばす : {item.table} / {item.label}（{item.reason}）")
            skipped += 1
            continue
        if args.dry_run:
            print(f"- 入れる : {item.table} / {item.label}（{item.reason}）※ --dry-run なので入れません")
            continue
        try:
            bq.insert(item.table, item.row)
        except bq.BigQueryUnavailable as exc:
            print(f"\nNG: {item.table} / {item.label} の登録で止まりました\n{exc}")
            return 1
        print(f"- 入れた : {item.table} / {item.label}（{item.reason}）")
        inserted += 1

    print()
    if args.dry_run:
        print("--dry-run なので何も登録していません。")
        return 0
    print(f"登録 {inserted} 行、飛ばし {skipped} 行。もう一度流しても同じ状態になります。")
    print("確認: stg_programs を見る（python 04_scripts/check_seed.py）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
