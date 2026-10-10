import 'package:flutter_local_notifications/flutter_local_notifications.dart';

/// نغمات التنبيه المتاحة (كل نغمة قناة إشعارات مستقلة في أندرويد).
enum ReminderTone { standard, vibrate, silent }

extension ReminderToneLabel on ReminderTone {
  String get label => switch (this) {
        ReminderTone.standard => 'نغمة النظام الافتراضية',
        ReminderTone.vibrate => 'اهتزاز فقط',
        ReminderTone.silent => 'إشعار صامت',
      };
}

class NotificationService {
  NotificationService._();
  static final instance = NotificationService._();

  final _plugin = FlutterLocalNotificationsPlugin();
  static const _reminderId = 1;

  static const _messages = [
    'حان وقت شرب الماء! كوب صغير يُنعش ذهنك ويقوّي تركيزك 💧',
    'جسمك يناديك: اشرب بعض الماء الآن 🌊',
    'رشفة ماء الآن تساعدك على الفهم والتركيز في الحصة 📚',
    'لا تنتظر العطش! اشرب كوباً من الماء 🥤',
  ];

  Future<void> init() async {
    const android = AndroidInitializationSettings('@drawable/ic_notification');
    await _plugin.initialize(const InitializationSettings(android: android));
  }

  Future<bool> requestPermission() async {
    final impl = _plugin.resolvePlatformSpecificImplementation<
        AndroidFlutterLocalNotificationsPlugin>();
    return await impl?.requestNotificationsPermission() ?? true;
  }

  NotificationDetails _details(ReminderTone tone) {
    final android = switch (tone) {
      ReminderTone.standard => const AndroidNotificationDetails(
          'water_standard', 'تذكير شرب الماء',
          channelDescription: 'تنبيهات دورية بنغمة النظام',
          importance: Importance.high,
          priority: Priority.high,
        ),
      ReminderTone.vibrate => const AndroidNotificationDetails(
          'water_vibrate', 'تذكير شرب الماء (اهتزاز)',
          channelDescription: 'تنبيهات دورية بالاهتزاز فقط',
          importance: Importance.high,
          priority: Priority.high,
          playSound: false,
          enableVibration: true,
        ),
      ReminderTone.silent => const AndroidNotificationDetails(
          'water_silent', 'تذكير شرب الماء (صامت)',
          channelDescription: 'تنبيهات دورية صامتة',
          importance: Importance.low,
          priority: Priority.low,
          playSound: false,
          enableVibration: false,
        ),
    };
    return NotificationDetails(android: android);
  }

  Future<void> schedule(int minutes, ReminderTone tone) async {
    await cancel();
    final msg = _messages[DateTime.now().minute % _messages.length];
    await _plugin.periodicallyShowWithDuration(
      _reminderId,
      'مذكر شرب الماء',
      msg,
      Duration(minutes: minutes),
      _details(tone),
      androidScheduleMode: AndroidScheduleMode.inexactAllowWhileIdle,
    );
  }

  Future<void> showTest(ReminderTone tone) => _plugin.show(
        99,
        'تنبيه تجريبي',
        'هكذا سيظهر تذكير شرب الماء 💧',
        _details(tone),
      );

  Future<void> cancel() => _plugin.cancelAll();
}
