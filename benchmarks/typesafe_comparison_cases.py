"""Frozen synthetic semantic decisions shared by TypeSafe and chat models."""

from benchmarks.typesafe_cases import cases
from benchmarks.typesafe_experiments import QUESTIONS


def choice(instructions, criteria):
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def comparison_cases():
    result = [dict(id=r["id"], family=r["experiment"], state=r["state"],
                   questions={"answer": QUESTIONS[r["experiment"]]["relation"]},
                   expected={"answer": r["expected"]}) for r in cases() if r["split"] == "holdout"]
    pairs = [
        (True, "Maya will send the draft on Friday.", "The draft will be sent by Maya on Friday."),
        (True, "We chose SQLite for local caching.", "The local cache will use SQLite."),
        (True, "The pilot costs $500 per month.", "Monthly pilot cost: five hundred dollars."),
        (True, "Customers cannot sign in after changing passwords.", "Password changes prevent customers from logging in."),
        (True, "Keep all recordings on the device.", "Audio files must stay local."),
        (True, "The launch is postponed until legal approves.", "We will wait for legal approval before launching."),
        (False, "Maya will send the draft on Friday.", "Maya will not send the draft on Friday."),
        (False, "The pilot costs $500 per month.", "The pilot costs $5000 per month."),
        (False, "Maya will send the draft on Friday.", "Omar will send the draft on Friday."),
        (False, "The release is scheduled for Tuesday.", "The release is scheduled for Thursday."),
        (False, "We might choose SQLite for local caching.", "We chose SQLite for local caching."),
        (False, "The deployment is approved if the audit passes.", "The deployment is approved."),
    ]
    for i, (same, left, right) in enumerate(pairs):
        result.append(dict(id=f"dedup_{i}", family="dedup", state=dict(left=left, right=right),
                           questions={"answer": choice("Do `left` and `right` express the same factual claim, including actor, numbers, time, negation, uncertainty and conditions?",
                                                        {"duplicate": "Same material claim; harmless paraphrase only.", "different": "At least one material detail or commitment differs."})},
                           expected={"answer": "duplicate" if same else "different"}))
    topics = [
        (False, "Customers cannot log in after resetting passwords. Authentication tokens expire during sessions.", "People are locked out when credentials change. The sign-in service needs a fix."),
        (False, "Audio recordings stay on the local device. We need encryption for stored meeting files.", "Disk protection should cover captured conversations. Confidential speech must remain offline."),
        (False, "Monthly spending exceeded the budget. Our vendor invoices are growing.", "Supplier bills cost too much. We must reduce recurring expenses."),
        (False, "The release needs better tests, deployment checks, rollback procedures and monitoring.", "Shipping software safely requires validation, production safeguards and recovery plans."),
        (False, "The dashboard navigation needs clearer labels, larger buttons and improved contrast for users.", "We should increase contrast and label the dashboard buttons more clearly."),
        (False, "We chose PostgreSQL for production data storage and will migrate the database next month.", "Actually, we should use SQLite for the database migration instead of PostgreSQL."),
        (True, "The release needs better tests, deployment checks, rollback procedures and monitoring.", "Our office lease expires next summer. Facilities needs new desks and parking spaces."),
        (True, "We are planning the release budget, release schedule, release owners and release marketing.", "Now let's plan the office budget, office schedule, office owners and office renovation."),
        (True, "We discussed recording latency, audio devices, microphone permissions and system capture.", "The hiring panel needs interviews, reference checks, salary ranges and offer approvals."),
        (True, "The sales team wants a pricing update, contract review, customer survey and pilot launch.", "Let's review earthquake preparedness, evacuation routes, assembly points and emergency supplies."),
        (True, "Database backups need encryption, retention policies, restoration drills and access controls.", "Lunch catering needs vegetarian meals, coffee, tables and serving utensils."),
        (True, "We need server capacity, memory limits, compute budgets and faster indexing for search.", "We need venue capacity, seating limits, catering budgets and faster check-in for guests."),
    ]
    for i, (shift, older, newer) in enumerate(topics):
        result.append(dict(id=f"topic_{i}", family="topic", state=dict(older=older, newer=newer),
                           questions={"answer": choice("Did the substantive discussion topic change from `older` to `newer`? A paraphrase, elaboration or changed decision about the same subject is not a topic change.",
                                                        {"same": "The same substantive subject continues.", "change": "Discussion moves to a different substantive subject."})},
                           expected={"answer": "change" if shift else "same"}))
    evidence = [
        ("Who owns the API review?", ["Maya accepted ownership of the API review.", "Omar asked who owns the API review.", "The API review is important."], 0),
        ("When is the launch?", ["The launch date has not been chosen.", "The launch is confirmed for Tuesday.", "We discussed launch risks."], 1),
        ("What is the approved budget?", ["Someone suggested $5,000.", "The budget proposal is under review.", "Finance approved a cap of $500."], 2),
        ("Which storage engine did we choose?", ["We selected SQLite.", "PostgreSQL was proposed but rejected.", "The choice of storage matters."], 0),
        ("Who owns the migration?", ["Maya might do it; we have not asked her.", "Migration ownership is undecided.", "Omar can suggest possible owners."], None),
        ("When will the audit finish?", ["The audit has started.", "We hope it finishes soon.", "The auditor has not provided a date."], None),
        ("Has legal approved the contract?", ["Legal is reviewing the contract.", "We cannot proceed without approval.", "The sales team likes the terms."], None),
        ("How many paid customers do we have?", ["There are 200 trial accounts.", "Revenue reporting is not ready.", "The paid count was not discussed."], None),
    ]
    for i, (query, texts, best) in enumerate(evidence):
        docs = {f"s{j}": text for j, text in enumerate(texts)}
        result.append(dict(id=f"answer_{i}", family="answer", state=dict(question=query, candidates=docs),
                           questions={"answer": choice("Which candidate in `candidates` explicitly answers `question` with an established fact? Proposals, hopes and unresolved discussion do not settle the answer.",
                                                        {**{key: value for key, value in docs.items()}, "none": "No candidate establishes an answer."})},
                           expected={"answer": "none" if best is None else f"s{best}"}))
    retrieval = [
        ("offline audio privacy", ["Offline audio privacy is the title of our open discussion; we made no policy.", "Recorded conversations must remain on the user's device and never leave it.", "The next release improves the theme."], 1),
        ("export crash fix", ["The export crash fix is still an open ticket; no remedy is given here.", "Release notes: export crash fix planned.", "To stop empty CSV uploads terminating the app, validate the header before parsing."], 2),
        ("approved pilot budget", ["Finance signed off on a five-hundred-dollar ceiling for the trial.", "The approved pilot budget needs discussing; no amount is recorded.", "Budget budget pilot pilot budget planning."], 0),
        ("API review owner", ["Who is the API review owner? We never settled this in this note.", "Maya accepted responsibility for evaluating both interfaces.", "API review agenda and background."], 1),
        ("database decision", ["We chose SQLite as the database.", "A database decision is needed soon.", "Database decision tracking template."], 0),
        ("launch date", ["Launch date is discussed but undecided.", "The launch date field is blank.", "Tuesday is the confirmed release day."], 2),
        ("refund policy", ["Refund policy discussion scheduled.", "We do not have a refund policy yet.", "Engineering tasks for next week."], None),
        ("retention duration", ["Retention duration is still undecided.", "We discussed retention duration without selecting a period.", "Audio files should be encrypted."], None),
    ]
    for i, (query, texts, best) in enumerate(retrieval):
        docs = {f"s{j}": text for j, text in enumerate(texts)}
        result.append(dict(id=f"retrieval_{i}", family="retrieval", state=dict(query=query, candidates=docs),
                           questions={"answer": choice("Which passage in `candidates` contains the most useful substantive answer about `query`? Prefer actual policy, resolution, owner or established fact over repeated keywords, agendas or unresolved questions.",
                                                        {**docs, "none": "No passage supplies the requested substantive information."})},
                           expected={"answer": "none" if best is None else f"s{best}"}))
    events = [
        ("Maya", "I will send the API comparison by Friday.", "action", "Maya", "Friday"),
        ("Omar", "I accept ownership of the migration and will finish it Tuesday.", "action", "Omar", "Tuesday"),
        ("Lee", "Nia agreed to review the draft by Monday.", "action", "Nia", "Monday"),
        ("Nia", "I'll investigate the timeout; we haven't set a deadline.", "action", "Nia", "none"),
        ("Maya", "We have decided to use SQLite for the cache.", "decision", None, None),
        ("Omar", "The team approved the $500 budget cap.", "decision", None, None),
        ("Lee", "We rejected the cloud-only option and will retain offline mode.", "decision", None, None),
        ("Nia", "Everyone agreed the launch should be postponed until legal approves.", "decision", None, None),
        ("Maya", "Who will own the migration? That is still unresolved.", "question", None, None),
        ("Omar", "We still need to decide whether the pilot can use customer data.", "question", None, None),
        ("Lee", "How long should we retain recordings? Nobody has answered that yet.", "question", None, None),
        ("Nia", "Can we run this on Windows? We need an answer from engineering.", "question", None, None),
        ("Maya", "Omar might investigate the timeout, but he has not agreed.", "none", None, None),
        ("Omar", "I investigated the timeout last week; that work is already complete.", "none", None, None),
        ("Lee", "For example, a task might say 'Maya will send it Friday'; this is only an example.", "none", None, None),
        ("Nia", "The coffee is good and the weather is sunny today.", "none", None, None),
    ]
    for i, (speaker, text, event, owner, deadline) in enumerate(events):
        questions = {
            "answer": choice("What new meeting event does `text` establish? Respect negation, hypothetical or quoted examples, completed past work and unaccepted suggestions.",
                             {"action": "A definite new or ongoing task is accepted or committed to by an identified person.", "decision": "An actual group choice, approval or rejection has been established.", "question": "An unresolved substantive question requiring an answer is raised.", "none": "No established action, decision or open question; includes small talk, hypothetical tasks and completed work."}),
            "owner": choice("If `text` establishes an accepted new/ongoing task, who owns it? First-person commitments refer to `speaker`. Do not assign a person who is merely proposed.",
                            {**{n: n for n in ("Maya", "Omar", "Lee", "Nia")}, "none": "No accepted task owner is established."}),
            "deadline": choice("If `text` establishes an accepted new/ongoing task, which deadline is explicitly committed to?", {"Friday": "Friday", "Tuesday": "Tuesday", "Monday": "Monday", "none": "No task deadline is committed to."}),
        }
        expected = {"answer": event}
        if event == "action":
            expected.update(owner=owner, deadline=deadline)
        result.append(dict(id=f"event_{i}", family="event", state=dict(speaker=speaker, text=text), questions=questions, expected=expected))
    return result
