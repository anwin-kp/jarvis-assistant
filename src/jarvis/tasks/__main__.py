"""See and manage task playbooks: python -m jarvis.tasks [list | show <id> | forget <id>]."""

import sys

from jarvis.tasks.playbooks import CURATED, LEARNED, NOTES, load_cards


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "list"
    cards = load_cards(CURATED) + load_cards(LEARNED, learned=True)
    if cmd == "list":
        for c in cards:
            d = c.data
            extra = f", {d.get('successes', 1)} successes" if d.get("source") == "learned" else ""
            notes = f", {len(c.notes)} corrections" if c.notes else ""
            print(f"  {d.get('status', 'verified'):9s} {d.get('source', 'curated'):7s} {c.id:28s} {d['title']}"
                  f"{extra}{notes}")
        print(f"\n  {len(cards)} playbooks ({sum(c.data.get('source') == 'learned' for c in cards)} learned)")
        return 0
    if cmd in ("show", "forget") and len(argv) > 1:
        card = next((c for c in cards if c.id == argv[1]), None)
        if not card:
            print(f"No playbook '{argv[1]}'.")
            return 1
        if cmd == "show":
            print(card.render())
            return 0
        if card.data.get("source") != "learned":
            print("Curated playbooks live in knowledge/tasks/ and are changed by editing (and reviewing) the file.")
            return 1
        card.path.unlink()
        (NOTES / f"{card.id}.yaml").unlink(missing_ok=True)
        print(f"Forgot the learned playbook '{card.id}'.")
        return 0
    print(__doc__)

    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
