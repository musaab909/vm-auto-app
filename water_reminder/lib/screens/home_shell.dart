import 'package:flutter/material.dart';

import '../services/app_state.dart';
import 'dashboard_screen.dart';
import 'reminders_screen.dart';
import 'settings_screen.dart';
import 'stats_screen.dart';

class HomeShell extends StatefulWidget {
  final AppState state;
  const HomeShell({super.key, required this.state});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    final pages = [
      DashboardScreen(state: widget.state),
      StatsScreen(state: widget.state),
      RemindersScreen(state: widget.state),
      SettingsScreen(state: widget.state),
    ];
    return Scaffold(
      body: AnimatedSwitcher(
        duration: const Duration(milliseconds: 250),
        child: KeyedSubtree(key: ValueKey(_index), child: pages[_index]),
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (i) => setState(() => _index = i),
        destinations: const [
          NavigationDestination(
              icon: Icon(Icons.home_outlined),
              selectedIcon: Icon(Icons.home),
              label: 'الرئيسية'),
          NavigationDestination(
              icon: Icon(Icons.insights_outlined),
              selectedIcon: Icon(Icons.insights),
              label: 'الإحصائيات'),
          NavigationDestination(
              icon: Icon(Icons.alarm_outlined),
              selectedIcon: Icon(Icons.alarm),
              label: 'التذكير'),
          NavigationDestination(
              icon: Icon(Icons.settings_outlined),
              selectedIcon: Icon(Icons.settings),
              label: 'الإعدادات'),
        ],
      ),
    );
  }
}
