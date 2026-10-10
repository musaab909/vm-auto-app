import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../models/profile.dart';
import 'hydration.dart';
import 'notifications.dart';

/// حالة التطبيق وكل البيانات المحفوظة محلياً على الجهاز.
class AppState extends ChangeNotifier {
  late SharedPreferences _prefs;

  StudentProfile? profile;
  int goal = 2000;
  bool remindersOn = true;
  bool autoInterval = true;
  int customInterval = 60;
  ReminderTone tone = ReminderTone.standard;

  /// إجمالي كل يوم: المفتاح بصيغة سنة-شهر-يوم.
  Map<String, int> history = {};
  List<DrinkEntry> todayEntries = [];
  String _todayKeyCache = '';

  static String keyOf(DateTime d) =>
      '${d.year}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';

  String get todayKey => keyOf(DateTime.now());
  int get todayTotal => history[todayKey] ?? 0;
  double get progress => goal == 0 ? 0 : (todayTotal / goal).clamp(0, 1).toDouble();
  int get intervalMinutes =>
      autoInterval ? Hydration.autoIntervalMinutes(goal) : customInterval;

  Future<void> load() async {
    _prefs = await SharedPreferences.getInstance();
    final p = _prefs.getString('profile');
    if (p != null) profile = StudentProfile.fromJson(jsonDecode(p));
    goal = _prefs.getInt('goal') ?? goal;
    remindersOn = _prefs.getBool('remindersOn') ?? true;
    autoInterval = _prefs.getBool('autoInterval') ?? true;
    customInterval = _prefs.getInt('customInterval') ?? 60;
    tone = ReminderTone.values[_prefs.getInt('tone') ?? 0];
    final h = _prefs.getString('history');
    if (h != null) {
      history = (jsonDecode(h) as Map<String, dynamic>)
          .map((k, v) => MapEntry(k, (v as num).toInt()));
    }
    _todayKeyCache = _prefs.getString('entriesDay') ?? '';
    final e = _prefs.getString('entries');
    if (e != null && _todayKeyCache == todayKey) {
      todayEntries = (jsonDecode(e) as List)
          .map((x) => DrinkEntry.fromJson(x as Map<String, dynamic>))
          .toList();
    }
  }

  Future<void> _save() async {
    await _prefs.setString('history', jsonEncode(history));
    await _prefs.setString('entries',
        jsonEncode(todayEntries.map((e) => e.toJson()).toList()));
    await _prefs.setString('entriesDay', todayKey);
  }

  void _rollDay() {
    if (_todayKeyCache != todayKey) {
      todayEntries = [];
      _todayKeyCache = todayKey;
    }
  }

  Future<void> saveProfile(StudentProfile p) async {
    profile = p;
    goal = Hydration.dailyGoal(p);
    await _prefs.setString('profile', jsonEncode(p.toJson()));
    await _prefs.setInt('goal', goal);
    notifyListeners();
    await applyReminders();
  }

  Future<void> addWater(int ml) async {
    _rollDay();
    history[todayKey] = todayTotal + ml;
    todayEntries.insert(0, DrinkEntry(DateTime.now(), ml));
    notifyListeners();
    await _save();
  }

  Future<void> undoLast() async {
    _rollDay();
    if (todayEntries.isEmpty) return;
    final last = todayEntries.removeAt(0);
    history[todayKey] = (todayTotal - last.ml).clamp(0, 1 << 30);
    notifyListeners();
    await _save();
  }

  Future<void> setReminders({bool? on, bool? auto, int? interval, ReminderTone? newTone}) async {
    if (on != null) remindersOn = on;
    if (auto != null) autoInterval = auto;
    if (interval != null) customInterval = interval;
    if (newTone != null) tone = newTone;
    await _prefs.setBool('remindersOn', remindersOn);
    await _prefs.setBool('autoInterval', autoInterval);
    await _prefs.setInt('customInterval', customInterval);
    await _prefs.setInt('tone', tone.index);
    notifyListeners();
    await applyReminders();
  }

  Future<void> applyReminders() async {
    try {
      if (remindersOn && profile != null) {
        await NotificationService.instance.schedule(intervalMinutes, tone);
      } else {
        await NotificationService.instance.cancel();
      }
    } catch (_) {
      // تجاهل أخطاء الجدولة كي لا يتعطل التطبيق.
    }
  }

  /// عدد الأيام المتتالية التي تحقق فيها الهدف (ينتهي باليوم أو بالأمس).
  int get streak {
    var d = DateTime.now();
    if ((history[keyOf(d)] ?? 0) < goal) d = d.subtract(const Duration(days: 1));
    var count = 0;
    while ((history[keyOf(d)] ?? 0) >= goal) {
      count++;
      d = d.subtract(const Duration(days: 1));
    }
    return count;
  }

  int get bestDay => history.values.fold(0, (a, b) => b > a ? b : a);

  int get daysGoalMet => history.values.where((v) => v >= goal).length;

  List<MapEntry<DateTime, int>> lastDays(int n) {
    final now = DateTime.now();
    return List.generate(n, (i) {
      final d = DateTime(now.year, now.month, now.day).subtract(Duration(days: n - 1 - i));
      return MapEntry(d, history[keyOf(d)] ?? 0);
    });
  }

  Future<void> resetAll() async {
    await NotificationService.instance.cancel();
    await _prefs.clear();
    profile = null;
    goal = 2000;
    remindersOn = true;
    autoInterval = true;
    customInterval = 60;
    tone = ReminderTone.standard;
    history = {};
    todayEntries = [];
    notifyListeners();
  }

  Future<void> clearHistory() async {
    history = {};
    todayEntries = [];
    notifyListeners();
    await _save();
  }
}
