"""Build the shipped snapshot database from the CSV export."""

from __future__ import annotations

import csv
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "data"))
DB = os.path.join(DATA, "fm_snapshot.db")

INT_WORDS = (
    "ID", "CA", "PA", "AGE", "HEIGHT_CM", "SALARY_PA", "SALARY_WEEKLY_RAW",
    "ASKING_PRICE", "ASKING_PRICE_RAW", "CURRENT_REPUTATION", "HOME_REPUTATION",
    "WORLD_REPUTATION", "CONDITION", "MATCH_SHARPNESS", "JADEDNESS", "MORALE",
    "CLUB_ID", "PLAYING_CLUB_ID", "UNIQUE_ID", "REPUTATION", "BALANCE",
    "TRANSFER_BUDGET", "PAYROLL_BUDGET", "TRAINING_FACILITIES", "YOUTH_FACILITIES",
    "YOUTH_COACHING", "YOUTH_RECRUITMENT", "CORPORATE_FACILITIES", "COMPETITION_ID",
    "JOB_ID", "HOME_CLUB_ID", "AWAY_CLUB_ID", "OPPONENT_CLUB_ID", "STAFF_INDEX",
    "CURRENT_WAGES_INCLUDING_BONUSES", "GOALKEEPER", "DEFENDER_LEFT",
    "DEFENDER_CENTRAL", "DEFENDER_RIGHT", "WING_BACK_LEFT", "DEFENSIVE_MIDFIELDER",
    "WING_BACK_RIGHT", "MIDFIELDER_LEFT", "MIDFIELDER_CENTRAL", "MIDFIELDER_RIGHT",
    "ATTACKING_MIDFIELDER_LEFT", "ATTACKING_MIDFIELDER_CENTRAL",
    "ATTACKING_MIDFIELDER_RIGHT", "STRIKER", "CROSSING", "DRIBBLING", "FINISHING",
    "HEADING", "LONG_SHOTS", "MARKING", "OFF_THE_BALL", "PASSING", "PENALTIES",
    "TACKLING", "VISION", "HANDLING", "AERIAL_ABILITY", "COMMAND_OF_AREA",
    "COMMUNICATION", "KICKING", "THROWING", "ANTICIPATION", "DECISIONS",
    "ONE_ON_ONES", "POSITIONING", "REFLEXES", "FIRST_TOUCH", "TECHNIQUE",
    "LEFT_FOOT", "RIGHT_FOOT", "FLAIR", "CORNERS", "TEAMWORK", "WORK_RATE",
    "LONG_THROWS", "ECCENTRICITY", "RUSHING_OUT", "TENDENCY_TO_PUNCH",
    "ACCELERATION", "FREE_KICKS", "STRENGTH", "STAMINA", "PACE", "JUMPING_REACH",
    "LEADERSHIP", "DIRTINESS", "BALANCE", "BRAVERY", "CONSISTENCY", "AGGRESSION",
    "AGILITY", "IMPORTANT_MATCHES", "INJURY_PRONENESS", "VERSATILITY",
    "NATURAL_FITNESS", "DETERMINATION", "COMPOSURE", "CONCENTRATION",
    "ADAPTABILITY", "AMBITION", "LOYALTY", "PRESSURE", "PROFESSIONALISM",
    "SPORTSMANSHIP", "TEMPERAMENT", "CONTROVERSY",
)

BOOL_WORDS = ("INJURED", "TRANSFER_LISTED", "LISTED_FOR_LOAN", "TRANSFER_AGREED", "ON_DUTY", "HOME")


def table_for(name: str) -> str:
    return name.lower()


def create_table(conn: sqlite3.Connection, name: str, headers: list[str]) -> None:
    cols = []
    for head in headers:
        col = head.lower()
        if col == "id":
            cols.append('"id" INTEGER PRIMARY KEY')
        elif head in INT_WORDS or head in BOOL_WORDS:
            cols.append(f'"{col}" INTEGER')
        elif head in ("SOURCE_ADDRESS", "RECORD_ADDRESS", "PLAYER_INDEX"):
            cols.append(f'"{col}" TEXT')
        else:
            cols.append(f'"{col}" TEXT')
    conn.execute(f'DROP TABLE IF EXISTS "{table_for(name)}"')
    conn.execute(f'CREATE TABLE "{table_for(name)}" ({", ".join(cols)})')


def coerce(head: str, value: str):
    if value == "":
        return None
    if head in BOOL_WORDS:
        return 1 if value.upper() == "TRUE" else 0
    if head in INT_WORDS:
        try:
            return int(float(value))
        except ValueError:
            return None
    return value


def load(conn: sqlite3.Connection, name: str, csv_name: str) -> int:
    path = os.path.join(DATA, csv_name)
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        headers = next(reader)
        create_table(conn, name, headers)
        placeholders = ",".join("?" * len(headers))
        cols = ",".join(f'"{h.lower()}"' for h in headers)
        rows = 0
        batch = []
        for record in reader:
            if len(record) != len(headers):
                continue
            batch.append([coerce(headers[i], record[i]) for i in range(len(headers))])
            rows += 1
            if len(batch) >= 2000:
                conn.executemany(f'INSERT INTO "{table_for(name)}" ({cols}) VALUES ({placeholders})', batch)
                batch.clear()
        if batch:
            conn.executemany(f'INSERT INTO "{table_for(name)}" ({cols}) VALUES ({placeholders})', batch)
    return rows


def main() -> None:
    if os.path.exists(DB):
        os.remove(DB)
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    counts = {}
    for name, csv_name in (
        ("PLAYERS", "players.csv"),
        ("CLUBS", "clubs.csv"),
        ("COMPETITIONS", "competitions.csv"),
        ("STAFF", "staff.csv"),
        ("FIXTURES", "fixtures.csv"),
        ("METADATA", "metadata.csv"),
    ):
        counts[name] = load(conn, name, csv_name)
        print(name, counts[name])
    conn.execute('CREATE INDEX ix_players_name ON players(name COLLATE NOCASE)')
    conn.execute('CREATE INDEX ix_players_club ON players(club)')
    conn.execute('CREATE INDEX ix_players_ca ON players(ca)')
    conn.execute('CREATE INDEX ix_players_uid ON players(unique_id)')
    conn.execute('CREATE INDEX ix_clubs_name ON clubs(name COLLATE NOCASE)')
    conn.execute('CREATE INDEX ix_clubs_competition ON clubs(competition)')
    conn.execute('CREATE INDEX ix_staff_club ON staff(club)')
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print("built", DB, os.path.getsize(DB))


if __name__ == "__main__":
    main()
