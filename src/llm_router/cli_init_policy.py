"""Interactive policy configuration wizard.

Guides users through selecting or creating a custom routing policy.
"""

from __future__ import annotations

import os

from llm_router.policy import RoutingPolicy, PolicyManager, get_policy_manager


def _prompt_choice(question: str, options: list[str], default: str | None = None) -> str:
    """Prompt user to choose from a list of options.
    
    Args:
        question: Question to ask
        options: List of options to choose from
        default: Default option if user just presses enter
        
    Returns:
        Selected option
    """
    default_marker = f" (default: {default})" if default else ""
    print(f"\n{question}{default_marker}")
    for i, opt in enumerate(options, 1):
        marker = " ← default" if opt == default else ""
        print(f"  {i}. {opt}{marker}")
    
    while True:
        try:
            choice = input("\nEnter your choice (1-{}): ".format(len(options))).strip()
            if not choice and default:
                return default
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx]
            print(f"❌ Please enter a number between 1 and {len(options)}")
        except ValueError:
            print("❌ Invalid input, please try again")


def _prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt user for a yes/no question.
    
    Args:
        question: Question to ask
        default: Default answer if user just presses enter
        
    Returns:
        True if yes, False if no
    """
    default_str = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{question} [{default_str}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("❌ Please enter 'y' or 'n'")


def _prompt_number(question: str, min_val: int = 0, max_val: int = 10, default: int = 5) -> int:
    """Prompt user for a number in a range.
    
    Args:
        question: Question to ask
        min_val: Minimum allowed value
        max_val: Maximum allowed value
        default: Default if user presses enter
        
    Returns:
        Selected number
    """
    while True:
        try:
            answer = input(f"{question} [{min_val}-{max_val}, default {default}]: ").strip()
            if not answer:
                return default
            val = int(answer)
            if min_val <= val <= max_val:
                return val
            print(f"❌ Please enter a number between {min_val} and {max_val}")
        except ValueError:
            print("❌ Please enter a valid number")


def run_init_policy_wizard() -> None:
    """Interactive policy setup wizard.
    
    Guides user through creating or selecting a routing policy.
    """
    pm = get_policy_manager()
    
    print("\n" + "=" * 60)
    print("LLM Router — Routing Policy Setup Wizard")
    print("=" * 60)
    print("\nThis wizard will help you choose or create a routing policy.")
    print("Policies control how aggressively tasks are routed to cheap models.\n")
    
    # Step 1: Choose preset or create custom
    choice = _prompt_choice(
        "What would you like to do?",
        [
            "Use a preset policy (aggressive/balanced/conservative)",
            "Create a custom policy",
            "List available policies and exit",
        ],
        default="Use a preset policy (aggressive/balanced/conservative)"
    )
    
    if choice == "List available policies and exit":
        _show_available_policies(pm)
        return
    
    if choice == "Use a preset policy (aggressive/balanced/conservative)":
        _select_preset_policy(pm)
    else:
        _create_custom_policy(pm)
    
    print("\n✅ Policy wizard complete!")


def _show_available_policies(pm: PolicyManager) -> None:
    """Show all available policies."""
    print("\n" + "=" * 60)
    print("Available Routing Policies")
    print("=" * 60)
    
    policies = pm.list_policies()
    if not policies:
        print("\n❌ No policies found!")
        return
    
    for name in sorted(policies.keys()):
        try:
            policy = pm.load_policy(name)
            print(f"\n📋 {name.upper()}")
            print("-" * 40)
            # Show first line of description
            desc_lines = policy.description.split('\n')
            for line in desc_lines:
                if line.strip():
                    print(f"   {line}")
            print(f"   Confidence threshold: {policy.confidence_threshold}/10")
            print(f"   Skip acknowledgements: {policy.skip_acknowledgements}")
            print(f"   Route coordination: {policy.route_coordination}")
            print(f"   Prefer Ollama: {policy.prefer_ollama}")
        except Exception as e:
            print(f"\n⚠️  Error loading {name}: {e}")


def _select_preset_policy(pm: PolicyManager) -> None:
    """Guide user to select a preset policy."""
    print("\n" + "=" * 60)
    print("Preset Policies")
    print("=" * 60)
    
    presets = ["aggressive", "balanced", "conservative"]
    print("\n🎯 Quick comparison:\n")
    
    print("AGGRESSIVE: Routes everything (high cost savings, may over-route simple tasks)")
    print("  ✓ Routes: 'yes', git commands, simple questions")
    print("  ✓ Cost savings: 60-75%")
    print("  → Use when: You want maximum savings\n")
    
    print("BALANCED: Routes moderate tasks (good balance of cost and quality)")
    print("  ✓ Routes: Code generation, analysis, research")
    print("  ✓ Skips: Simple acknowledgements")
    print("  ✓ Cost savings: 35-45%")
    print("  → Use when: You want cost savings without sacrificing quality\n")
    
    print("CONSERVATIVE: Routes only complex tasks (minimal over-routing)")
    print("  ✓ Routes: Deep analysis, architecture decisions")
    print("  ✓ Skips: Simple questions and acknowledgements")
    print("  ✓ Cost savings: 10-15%")
    print("  → Use when: You prefer quality over cost\n")
    
    choice = _prompt_choice(
        "Which policy would you like to use?",
        presets,
        default="balanced"
    )
    
    pm.set_active_policy(choice)
    os.environ["LLM_ROUTER_POLICY"] = choice
    
    print(f"\n✅ Selected policy: {choice}")
    print(f"   Set LLM_ROUTER_POLICY={choice} in your environment")


def _create_custom_policy(pm: PolicyManager) -> None:
    """Guide user through creating a custom policy."""
    print("\n" + "=" * 60)
    print("Create Custom Policy")
    print("=" * 60)
    print("\nI'll ask you some questions about your routing preferences.")
    print("Your answers will create a custom policy tailored to your needs.\n")
    
    # Step 1: Policy name
    while True:
        name = input("Policy name (e.g., 'my-policy'): ").strip().lower()
        if not name:
            print("❌ Policy name cannot be empty")
            continue
        if not name.replace("-", "_").replace("_", "").isalnum():
            print("❌ Policy name can only contain letters, numbers, hyphens, underscores")
            continue
        name = name.replace("-", "_")
        break
    
    # Step 2: How aggressive should routing be?
    aggressiveness = _prompt_number(
        "How aggressive should routing be?",
        min_val=1,
        max_val=10,
        default=5
    )
    
    # Convert aggressiveness to threshold
    # 1-3 = aggressive (threshold 2)
    # 4-6 = balanced (threshold 4)
    # 7-10 = conservative (threshold 6-8)
    if aggressiveness <= 3:
        threshold = 2
    elif aggressiveness <= 6:
        threshold = 4
    else:
        threshold = min(8, 4 + (aggressiveness - 6))
    
    # Step 3: Route simple acknowledgements?
    skip_acks = _prompt_yes_no(
        "Skip routing for simple acknowledgements (yes, ok, thanks)?",
        default=True if aggressiveness > 6 else False
    )
    
    # Step 4: Route coordination tasks?
    route_coord = _prompt_yes_no(
        "Route coordination tasks (git push, deploy, run tests)?",
        default=True if aggressiveness <= 3 else False
    )
    
    # Step 5: Prefer Ollama (local)?
    prefer_ollama = _prompt_yes_no(
        "Prefer local Ollama models over Claude subscription?",
        default=True if aggressiveness <= 6 else False
    )
    
    # Create the policy
    description = f"Custom policy created by wizard (aggressiveness={aggressiveness}/10)"
    
    policy = RoutingPolicy(
        name=name,
        description=description,
        confidence_threshold=threshold,
        skip_patterns=["^/(help|clear|login|version)"],
        skip_acknowledgements=skip_acks,
        route_coordination=route_coord,
        prefer_ollama=prefer_ollama,
    )
    
    # Save and activate
    path = pm.save_custom_policy(policy)
    pm.set_active_policy(name)
    os.environ["LLM_ROUTER_POLICY"] = name
    
    print(f"\n✅ Created custom policy: {name}")
    print(f"   Saved to: {path}")
    print("\n📋 Policy settings:")
    print(f"   Aggressiveness: {aggressiveness}/10")
    print(f"   Confidence threshold: {threshold}")
    print(f"   Skip acknowledgements: {skip_acks}")
    print(f"   Route coordination: {route_coord}")
    print(f"   Prefer Ollama: {prefer_ollama}")
    print("\n💡 To use this policy:")
    print(f"   export LLM_ROUTER_POLICY={name}")
    print("   Or set in ~/.env or ~/.llm-router/config.yaml")
