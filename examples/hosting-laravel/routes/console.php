<?php

use App\Models\Note;
use Illuminate\Support\Facades\Schedule;

// Планировщик запускает задача cron из airlok.yml (php artisan schedule:run каждую минуту).
Schedule::call(fn () => Note::where('created_at', '<', now()->subMonth())->delete())->daily();
