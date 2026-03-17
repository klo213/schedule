# Coach Intake Form Setup

Use this form when coaches need to submit game or practice change requests.

## Form Title

`Field Scheduler Change Request`

## Form Description

`Submit one request per game or practice that needs to be created, moved, or updated. Please include complete date, time, field, and umpire details.`

## Required Questions

Create these questions with these exact labels:

1. `Coach Full Name`
2. `Coach Mobile Number`
3. `Coach Email`
4. `Team`
5. `Event Type`
6. `Opponent Team`
7. `Requested Date`
8. `Requested Start Time`
9. `Requested End Time`
10. `Preferred Field`
11. `Umpire Required?`
12. `Is this request urgent (within 48 hours)?`
13. `Reason for change`

## Recommended Question Types

1. `Coach Full Name`: Short answer
2. `Coach Mobile Number`: Short answer
3. `Coach Email`: Short answer with email validation
4. `Team`: Dropdown or short answer
5. `Event Type`: Multiple choice
   Options:
   - `Game`
   - `Practice`
6. `Opponent Team`: Short answer
7. `Requested Date`: Date
8. `Requested Start Time`: Time
9. `Requested End Time`: Time
10. `Preferred Field`: Dropdown
    Suggested options:
    - `Fenway 1`
    - `Fenway 2`
    - `Fenway 5`
    - `Scandia`
    - `Scandia (Wayne Erickson Memorial Park)`
    - `FLAMS 1`
    - `FLAMS 2`
    - `Kulenkamp 1`
    - `Kulenkamp 2`
    - `Kulenkamp 3`
    - `Kulenkamp 4`
    - `FW 5 Batting Cage`
    - `KK Batting Cage`
    - `KK Bullpen 3`
    - `KK Bullpen 4`
11. `Umpire Required?`: Multiple choice
    Options:
    - `Yes`
    - `No`
12. `Is this request urgent (within 48 hours)?`: Multiple choice
    Options:
    - `Yes`
    - `No`
13. `Reason for change`: Paragraph

## Response Sheet Expectations

The linked response sheet should produce columns matching the sample in [`samples/coach_form_responses_template.csv`](../samples/coach_form_responses_template.csv).

The current config in [`config/config.yaml`](../config/config.yaml) maps these form/sheet columns:

- `Coach Full Name`
- `Coach Email`
- `Team`
- `Event Type`
- `Preferred Field`
- `Requested Date`
- `Requested Start Time`
- `Requested End Time`
- `Umpire Required?`
- `Reason for change`

Additional fields such as `Coach Mobile Number`, `Opponent Team`, and `Is this request urgent (within 48 hours)?` are still worth collecting now because they are operationally useful and already represented in the response template.
