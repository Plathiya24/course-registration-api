import re

from bs4 import BeautifulSoup
from fastapi import FastAPI, File, HTTPException, UploadFile

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