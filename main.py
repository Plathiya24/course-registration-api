import re

from bs4 import BeautifulSoup
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from pydantic import BaseModel
app = FastAPI()

# In-memory course storage
courses = {}


def normalize_course_code(code: str) -> str:
    """
    Convert course codes such as:
    'COSC 3506' -> 'COSC3506'
    'cosc3506'  -> 'COSC3506'
    """
    return re.sub(r"[^A-Za-z0-9]", "", code).upper()


def extract_course_codes(text: str):
    """
    Extract course codes from prerequisite or cross-listed text.

    Examples:
    'COSC 2007' -> ['COSC2007']
    'Requires COSC 1046 and either COSC 1047 or ITEC 1047'
        -> ['COSC1046', 'COSC1047', 'ITEC1047']
    'None' -> []
    """
    if not text:
        return []

    text = text.strip()

    if text.lower() in {"none", "n/a", "na", ""}:
        return []

    pattern = r"\b([A-Za-z]{2,10})\s*[- ]?\s*(\d{3,4}[A-Za-z]?)\b"

    matches = re.findall(pattern, text)

    result = []

    for department, number in matches:
        code = f"{department}{number}".upper()

        if code not in result:
            result.append(code)

    return result


def parse_credits(value: str):
    """
    Convert credit values to numbers when possible.
    """
    value = value.strip()

    try:
        number = float(value)

        if number.is_integer():
            return int(number)

        return number

    except ValueError:
        return value


@app.get("/")
def home():
    return {
        "message": "Course Catalog API is running"
    }


@app.post("/api/v1/admin/catalog/import")
async def import_catalog(file: UploadFile = File(...)):
    content = await file.read()

    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError:
        html = content.decode("latin-1")

    soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table")

    if table is None:
        raise HTTPException(
            status_code=400,
            detail="No course table found in uploaded HTML"
        )

    rows = table.find_all("tr")

    if not rows:
        raise HTTPException(
            status_code=400,
            detail="Course table is empty"
        )

    # Read column names from the first row.
    header_cells = rows[0].find_all(["th", "td"])

    headers = [
        cell.get_text(" ", strip=True).lower()
        for cell in header_cells
    ]

    # Locate each required column by its heading.
    try:
        code_index = headers.index("course code")
        title_index = headers.index("title")
        credits_index = headers.index("credits")
        prerequisites_index = headers.index("prerequisites")
        cross_listed_index = headers.index("cross-listed")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Catalog does not contain the required columns"
        )

    # Remove previously imported data.
    courses.clear()

    # Process all rows after the header.
    for row in rows[1:]:
        cells = row.find_all("td")

        if not cells:
            continue

        values = [
            cell.get_text(" ", strip=True)
            for cell in cells
        ]

        required_index = max(
            code_index,
            title_index,
            credits_index,
            prerequisites_index,
            cross_listed_index,
        )

        if len(values) <= required_index:
            continue

        raw_code = values[code_index]

        course_code = normalize_course_code(raw_code)

        if not course_code:
            continue

        course = {
            "course_code": course_code,
            "title": values[title_index],
            "credits": parse_credits(values[credits_index]),
            "prerequisites": extract_course_codes(
                values[prerequisites_index]
            ),
            "cross_listed": extract_course_codes(
                values[cross_listed_index]
            ),
        }

        courses[course_code] = course

    return {
        "message": "Catalog imported successfully",
        "courses_imported": len(courses)
    }


@app.get("/api/v1/catalog/courses/{course_code}")
def get_course(course_code: str):
    normalized_code = normalize_course_code(course_code)

    course = courses.get(normalized_code)

    if course is None:
        raise HTTPException(
            status_code=404,
            detail="Course not found"
        )

    return course

# ============================================================
# PHASE 2 - STUDENT ACADEMIC PROFILE
# ============================================================

# Separate per-student state.
# Do not reuse the Phase 1 catalog dictionary for this.
app.state.students = {}

VALID_HISTORY_STATUSES = {
    "Completed",
    "In-Progress",
    "Attempted",
}


class HistoryCourse(BaseModel):
    course_code: str
    term: str
    credits_earned: int
    status: str


class HistoryPayload(BaseModel):
    history: list[HistoryCourse]


class PlannedCourse(BaseModel):
    course_code: str
    term: str


class PlanPayload(BaseModel):
    planned_courses: list[PlannedCourse]


def model_to_dict(model):
    if hasattr(model, "model_dump"):
        return model.model_dump()

    return model.dict()


def normalize_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def find_column(headers, accepted_names):
    for index, header in enumerate(headers):
        if header in accepted_names:
            return index

    return None


def parse_credits(text: str) -> int:
    text = text.strip()

    if not text:
        return 0

    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return 0

    try:
        return int(float(text))
    except ValueError:
        return 0


def grade_information_rank(grade: str) -> int:
    """
    Canonical duplicate preference:

    numeric grade > letter grade > P/blank
    """

    grade = grade.strip().upper()
    compact_grade = grade.replace(" ", "")

    # Numeric grade, for example:
    # 85
    # 85.5
    # 85%
    if re.fullmatch(r"\d+(?:\.\d+)?%?", compact_grade):
        return 3

    # Letter grade, for example:
    # A
    # A-
    # B+
    if re.fullmatch(r"[A-F][+-]?", compact_grade):
        return 2

    # Pass or blank
    if compact_grade == "P" or compact_grade == "":
        return 1

    return 0


def parse_student_transcript(html: str):
    soup = BeautifulSoup(html, "html.parser")

    # Key:
    # (course_code, term)
    #
    # This automatically supports duplicate removal.
    deduplicated = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")

        if not rows:
            continue

        header_row_position = None
        indexes = None

        # Find the transcript header row dynamically.
        for row_number, row in enumerate(rows):
            cells = row.find_all(["th", "td"])

            headers = [
                normalize_header(cell.get_text(" ", strip=True))
                for cell in cells
            ]

            status_index = find_column(
                headers,
                {"status"},
            )

            course_index = find_column(
                headers,
                {"course", "coursecode"},
            )

            grade_index = find_column(
                headers,
                {"grade"},
            )

            term_index = find_column(
                headers,
                {"term"},
            )

            credits_index = find_column(
                headers,
                {"credit", "credits"},
            )

            required_indexes = [
                status_index,
                course_index,
                grade_index,
                term_index,
                credits_index,
            ]

            if all(index is not None for index in required_indexes):
                header_row_position = row_number

                indexes = {
                    "status": status_index,
                    "course": course_index,
                    "grade": grade_index,
                    "term": term_index,
                    "credits": credits_index,
                }

                break

        # Ignore unrelated HTML tables.
        if header_row_position is None:
            continue

        highest_required_index = max(indexes.values())

        for row in rows[header_row_position + 1:]:
            cells = row.find_all(["td", "th"])

            if len(cells) <= highest_required_index:
                continue

            values = [
                cell.get_text(" ", strip=True)
                for cell in cells
            ]

            course_status = values[indexes["status"]]
            course_code = values[indexes["course"]]
            grade = values[indexes["grade"]]
            term = values[indexes["term"]]
            credits = parse_credits(values[indexes["credits"]])

            # Canonical rule:
            # only these three statuses are past courses.
            if course_status not in VALID_HISTORY_STATUSES:
                continue

            # Empty term means requirement placeholder,
            # so it must not be included.
            if not term:
                continue

            if not course_code:
                continue

            key = (course_code, term)

            record = {
                "course_code": course_code,
                "term": term,
                "credits_earned": credits,
                "status": course_status,
            }

            new_grade_rank = grade_information_rank(grade)

            if key not in deduplicated:
                deduplicated[key] = {
                    "record": record,
                    "grade_rank": new_grade_rank,
                    "credits": credits,
                }

                continue

            current = deduplicated[key]

            # Better grade information wins.
            if new_grade_rank > current["grade_rank"]:
                deduplicated[key] = {
                    "record": record,
                    "grade_rank": new_grade_rank,
                    "credits": credits,
                }

            # Same grade quality:
            # higher credits wins.
            elif (
                new_grade_rank == current["grade_rank"]
                and credits > current["credits"]
            ):
                deduplicated[key] = {
                    "record": record,
                    "grade_rank": new_grade_rank,
                    "credits": credits,
                }

    return [
        entry["record"]
        for entry in deduplicated.values()
    ]


def get_student_or_404(student_id: str):
    student = app.state.students.get(student_id)

    if student is None:
        raise HTTPException(
            status_code=404,
            detail="Student not found",
        )

    return student


# ------------------------------------------------------------
# HISTORY IMPORT
# ------------------------------------------------------------

@app.post(
    "/api/v1/students/{student_id}/history/import",
    status_code=status.HTTP_201_CREATED,
)
async def import_student_history(
    student_id: str,
    file: UploadFile = File(...),
):
    content = await file.read()

    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError:
        html = content.decode("latin-1")

    history = parse_student_transcript(html)

    existing_student = app.state.students.get(student_id)

    if existing_student is None:
        app.state.students[student_id] = {
            "history": history,
            "plan": [],
        }
    else:
        # Re-import replaces history but preserves plan.
        existing_student["history"] = history

    return {
        "status": "success",
        "past_courses_imported": len(history),
    }


# ------------------------------------------------------------
# HISTORY PUT
# ------------------------------------------------------------

@app.put("/api/v1/students/{student_id}/history")
def update_student_history(
    student_id: str,
    payload: HistoryPayload,
):
    student = get_student_or_404(student_id)

    student["history"] = [
        model_to_dict(course)
        for course in payload.history
    ]

    return {
        "status": "success",
        "message": "Academic history updated successfully",
    }


# ------------------------------------------------------------
# HISTORY DELETE
# ------------------------------------------------------------

@app.delete("/api/v1/students/{student_id}/history")
def delete_student_history(student_id: str):
    student = get_student_or_404(student_id)

    student["history"] = []

    return {
        "status": "success",
        "message": "Academic history cleared successfully",
    }


# ------------------------------------------------------------
# PLAN POST
# ------------------------------------------------------------

@app.post("/api/v1/students/{student_id}/plan")
def create_student_plan(
    student_id: str,
    payload: PlanPayload,
):
    student = get_student_or_404(student_id)

    plan = [
        model_to_dict(course)
        for course in payload.planned_courses
    ]

    student["plan"] = plan

    return {
        "status": "success",
        "planned_courses_saved": len(plan),
    }


# ------------------------------------------------------------
# PLAN PUT
# ------------------------------------------------------------

@app.put("/api/v1/students/{student_id}/plan")
def update_student_plan(
    student_id: str,
    payload: PlanPayload,
):
    student = get_student_or_404(student_id)

    plan = [
        model_to_dict(course)
        for course in payload.planned_courses
    ]

    student["plan"] = plan

    return {
        "status": "success",
        "planned_courses_saved": len(plan),
    }


# ------------------------------------------------------------
# PLAN DELETE
# ------------------------------------------------------------

@app.delete("/api/v1/students/{student_id}/plan")
def delete_student_plan(student_id: str):
    student = get_student_or_404(student_id)

    student["plan"] = []

    return {
        "status": "success",
        "message": "Plan cleared successfully",
    }


# ------------------------------------------------------------
# UNIFIED PROFILE
# ------------------------------------------------------------

@app.get("/api/v1/students/{student_id}/profile")
def get_student_profile(student_id: str):
    student = get_student_or_404(student_id)

    # The assignment requires EXACTLY these 3 top-level keys.
    return {
        "student_id": student_id,
        "history": student["history"],
        "plan": student["plan"],
    }